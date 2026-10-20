"""Limit synchronous Slime to complete outer iterations without editing its code.

The integration deliberately targets two public, Slime-specific seams.  It does
not import Slime, Ray, or GPU libraries during Python startup.
"""

from __future__ import annotations

import functools
import ast
import importlib.abc
import importlib.machinery
import inspect
import json
import os
from pathlib import Path
import sys
from types import ModuleType
from typing import Any


class SlimeAdapterError(RuntimeError):
    """The selected Slime entry point is incompatible with this adapter."""


_MODULES = {"slime.ray.utils", "slime.ray.placement_group"}
_finder: _SlimeFinder | None = None


def _validate_driver(path: Path) -> None:
    """Fail before application imports if the known synchronous seam changed."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise SlimeAdapterError(f"cannot inspect Slime entry point {path}: {exc}") from exc
    imports = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "slime.ray.placement_group"
        for alias in node.names
        if alias.name == "create_training_models"
    }
    train = next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "train"), None)
    if not imports or train is None:
        raise SlimeAdapterError("unsupported train.py: expected the synchronous Slime train() entry point")

    def attribute(node: ast.AST, name: str) -> bool:
        return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "args" and node.attr == name

    initialized = False
    for statement in train.body:
        if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call):
            function = statement.value.func
            if isinstance(function, ast.Name) and function.id in imports:
                initialized = True
        if not isinstance(statement, ast.For) or not isinstance(statement.iter, ast.Call):
            continue
        call = statement.iter
        if (
            initialized and isinstance(call.func, ast.Name) and call.func.id == "range"
            and len(call.args) == 2 and not call.keywords
            and attribute(call.args[0], "start_rollout_id") and attribute(call.args[1], "num_rollout")
        ):
            return
    raise SlimeAdapterError("unsupported train.py: expected model initialization before range(args.start_rollout_id, args.num_rollout)")


def _record_budget(config: Any, original_stop: Any, count: int) -> None:
    directory = os.environ.get("CALLNAV_RUN_DIR")
    if not directory:
        return
    output = Path(directory) / "adapter"
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{os.getpid()}.json"
    temporary = path.with_suffix(".json.tmp")
    marker = {
        "adapter": "slime", "pid": os.getpid(), "status": "configured",
        "iterations": count, "original_num_rollout": original_stop,
        "start_rollout_id": config.start_rollout_id, "stop_rollout_id": config.num_rollout,
    }
    temporary.write_text(json.dumps(marker), encoding="utf-8")
    os.replace(temporary, path)


def _iterations() -> int:
    value = os.environ.get("CALLNAV_ITERATIONS", "1")
    try:
        count = int(value)
    except ValueError as exc:
        raise SlimeAdapterError("CALLNAV_ITERATIONS must be a positive integer") from exc
    if count < 1:
        raise SlimeAdapterError("CALLNAV_ITERATIONS must be a positive integer")
    return count


def _merge_pythonpath(*values: str) -> str:
    paths: list[str] = []
    for value in values:
        if not value:
            continue
        for path in value.split(os.pathsep):
            if path not in paths:
                paths.append(path)
    return os.pathsep.join(paths)


def _patch_environment(module: ModuleType) -> None:
    original = getattr(module, "add_default_ray_env_vars", None)
    if not callable(original):
        raise SlimeAdapterError("Slime has no supported add_default_ray_env_vars helper")
    if getattr(original, "__callnav_slime__", False):
        return

    @functools.wraps(original)
    def add_default_ray_env_vars(*args: Any, **kwargs: Any) -> dict[str, str]:
        result = dict(original(*args, **kwargs))
        # Ray actors can replace the job's environment.  Carry the recording
        # opt-in and startup bootstrap through Slime's own merge helper.
        result.update({name: value for name, value in os.environ.items() if name.startswith("CALLNAV_")})
        result["PYTHONPATH"] = _merge_pythonpath(
            os.environ.get("PYTHONPATH", ""), result.get("PYTHONPATH", "")
        )
        return result

    add_default_ray_env_vars.__callnav_slime__ = True  # type: ignore[attr-defined]
    module.add_default_ray_env_vars = add_default_ray_env_vars


def _patch_training(module: ModuleType) -> None:
    original = getattr(module, "create_training_models", None)
    if not callable(original):
        raise SlimeAdapterError("Slime has no supported create_training_models helper")
    if getattr(original, "__callnav_slime__", False):
        return
    parameters = inspect.signature(original).parameters
    if not {"args", "pgs", "rollout_manager"}.issubset(parameters):
        raise SlimeAdapterError("Unsupported Slime create_training_models signature")
    count = _iterations()

    @functools.wraps(original)
    def create_training_models(*args: Any, **kwargs: Any) -> Any:
        config = args[0] if args else kwargs.get("args")
        if config is None:
            raise SlimeAdapterError("Slime create_training_models received no args configuration")
        if getattr(config, "num_rollout", None) == 0:
            raise SlimeAdapterError("Slime evaluation-only mode cannot record a training iteration")
        original_stop = getattr(config, "num_rollout", None)
        result = original(*args, **kwargs)
        start = getattr(config, "start_rollout_id", None)
        if not isinstance(start, int) or isinstance(start, bool) or start < 0:
            raise SlimeAdapterError("Slime did not resolve a valid start_rollout_id after model initialization")
        # num_rollout is an exclusive stop index, not a number of steps.  The
        # checkpoint start is known only after original model initialization.
        config.num_rollout = start + count
        _record_budget(config, original_stop, count)
        return result

    create_training_models.__callnav_slime__ = True  # type: ignore[attr-defined]
    module.create_training_models = create_training_models


def _patch(module: ModuleType) -> None:
    if module.__name__ == "slime.ray.utils":
        _patch_environment(module)
    elif module.__name__ == "slime.ray.placement_group":
        _patch_training(module)


class _SlimeLoader(importlib.abc.Loader):
    def __init__(self, loader: Any):
        self.loader = loader

    def create_module(self, spec: Any) -> ModuleType | None:
        create = getattr(self.loader, "create_module", None)
        return create(spec) if create else None

    def exec_module(self, module: ModuleType) -> None:
        self.loader.exec_module(module)
        _patch(module)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.loader, name)


class _SlimeFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname == "train_async":
            raise SlimeAdapterError("The Slime adapter supports train.py; train_async.py is not supported")
        if fullname == "train":
            spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
            if spec is not None and spec.origin:
                _validate_driver(Path(spec.origin))
            return spec
        if fullname not in _MODULES:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _SlimeLoader(spec.loader)
        return spec


def install() -> None:
    """Install once from the recording bootstrap, before Slime is imported.

    A direct asynchronous entry point exits during site initialization: ordinary
    exceptions from ``sitecustomize`` are printed by Python but then ignored.
    """
    global _finder
    if Path(sys.argv[0]).name == "train_async.py":
        raise SystemExit("callnav: Slime train_async.py is not supported; use synchronous train.py")
    try:
        _iterations()
        if Path(sys.argv[0]).name == "train.py":
            _validate_driver(Path(sys.argv[0]))
    except SlimeAdapterError as exc:
        raise SystemExit(f"callnav: {exc}") from exc
    if _finder is not None:
        return
    # Late installation would leave copied function references unpatched.  The
    # normal sitecustomize path runs before any application import.
    if "slime.ray.placement_group" in sys.modules or "slime.ray.actor_group" in sys.modules:
        raise SystemExit("callnav: enable the Slime adapter before importing Slime")
    _finder = _SlimeFinder()
    sys.meta_path.insert(0, _finder)
    utils = sys.modules.get("slime.ray.utils")
    if utils is not None:
        _patch(utils)
