"""Opt-in Ray actor navigation, confirmed by actual worker function entries.

Ray private hooks are isolated here. Tested against Ray 2.58; no arguments or
return values are recorded. A submission alone never creates a navigable edge.
"""

from __future__ import annotations

import ast
from contextvars import ContextVar
import dis
import functools
import importlib.abc
import importlib.machinery
import inspect
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time

_collector = None
_finder = None
_pending = ContextVar("callnav_ray_execution", default=None)
_write_lock = threading.RLock()
_warnings = set()
_MODULES = {"ray", "ray.actor", "ray._private.function_manager"}


def _event(event):
    if _collector is None:
        return
    try:
        event = dict(
            event, run_id=_collector.run_id, pid=os.getpid(), host=socket.gethostname(),
            timestamp=time.time(), python=sys.version.split()[0],
        )
        directory = Path(_collector.run_dir) / "ray"
        directory.mkdir(parents=True, exist_ok=True)
        host = re.sub(r"[^a-zA-Z0-9_.-]", "_", socket.gethostname())
        with _write_lock, (directory / f"{host}-{os.getpid()}.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=True) + "\n")
    except Exception as exc:
        _warn("event_write_failed", str(exc))


def _warn(code, message):
    if code in _warnings:
        return
    _warnings.add(code)
    print(f"callnav: Ray tracing warning: {message}", file=sys.stderr)
    if code != "event_write_failed":
        _event({"type": "diagnostic", "code": code, "message": message})


def _merge_paths(*values):
    paths = []
    for value in values:
        for path in (value or "").split(os.pathsep):
            if path and path not in paths:
                paths.append(path)
    return os.pathsep.join(paths)


def _patch_ray(module):
    original = getattr(module, "init", None)
    if not callable(original) or getattr(original, "__callnav_ray__", False):
        return

    @functools.wraps(original)
    def init(*args, **kwargs):
        runtime_env = kwargs.get("runtime_env")
        if runtime_env is None or isinstance(runtime_env, dict):
            merged = dict(runtime_env or {})
            env_vars = dict(merged.get("env_vars") or {})
            env_vars.update({key: value for key, value in os.environ.items() if key.startswith("CALLNAV_")})
            env_vars["PYTHONPATH"] = _merge_paths(os.environ.get("PYTHONPATH"), env_vars.get("PYTHONPATH"))
            merged["env_vars"] = env_vars
            kwargs["runtime_env"] = merged
        else:
            _warn("unsupported_runtime_env", "ray.init runtime_env is not a dictionary; worker coverage may be incomplete")
        if module.is_initialized():
            _warn("already_initialized", "Ray was already initialized; existing workers may not be instrumented")
        return original(*args, **kwargs)

    init.__callnav_ray__ = True
    module.init = init
    _event({"type": "installed", "version": getattr(module, "__version__", "unknown")})
    if module.is_initialized():
        _warn("already_initialized", "Ray was already initialized; existing workers may not be instrumented")


def _source_frame():
    frame = sys._getframe(1)
    while frame is not None:
        if _collector.path(frame.f_code.co_filename):
            return frame
        frame = frame.f_back
    return None


def _submission_site(frame, method_name):
    with _collector.lock:
        path = _collector.path(frame.f_code.co_filename)
        source = _collector.source(path)
        if source is None:
            return None
        code = frame.f_code
        if code not in _collector.instructions:
            operations = list(dis.get_instructions(code))
            _collector.instructions[code] = ([item.offset for item in operations], operations)
        caller, expression = source.callsite(
            code, frame.f_lasti, frame.f_lineno, _collector.instructions[code]
        )
        if caller.get("ambiguous") and expression is None:
            # CPython may position a multiline chained CALL at just its final
            # remote token, while the AST Call begins at the options receiver.
            operation = next((item for item in reversed(_collector.instructions[code][1])
                              if item.offset <= frame.f_lasti), None)
            positions = getattr(operation, "positions", None)
            if operation and operation.opname.startswith("CALL") and positions:
                candidates = [
                    call for call in source.calls
                    if isinstance(call.func, ast.Attribute) and call.func.attr == "remote"
                    and (call.func.end_lineno, call.func.end_col_offset - len("remote"))
                    == (positions.lineno, positions.col_offset)
                    and (call.end_lineno, call.end_col_offset)
                    == (positions.end_lineno, positions.end_col_offset)
                ]
                if len(candidates) == 1:
                    expression = candidates[0].func
                    caller.update(source.callee_range(candidates[0]), precision="instruction")
                    caller.pop("ambiguous", None)
        # Source.callsite normally highlights just "remote". Include the actual
        # method token as well so both run and remote support Ctrl+click.
        if isinstance(expression, ast.Attribute) and expression.attr == "remote":
            candidate = expression.value
            if isinstance(candidate, ast.Call) and isinstance(candidate.func, ast.Attribute) and candidate.func.attr == "options":
                candidate = candidate.func.value
            if (isinstance(candidate, ast.Attribute) and candidate.attr == method_name
                    and candidate.end_lineno == caller["line"]):
                start = source.range(candidate)
                caller["line"] = start["end_line"]
                caller["column"] = start["end_column"] - len(method_name.encode("utf-16-le")) // 2
            elif (method_name == "__init__" and isinstance(candidate, (ast.Name, ast.Attribute))
                  and candidate.end_lineno == caller["line"]):
                start = source.range(candidate)
                caller["line"], caller["column"] = start["line"], start["column"]
                if isinstance(candidate, ast.Attribute):
                    caller["line"] = start["end_line"]
                    caller["column"] = start["end_column"] - len(candidate.attr.encode("utf-16-le")) // 2
        return caller, {path: {"sha256": source.sha256}}


def _task_ids(result):
    refs = result if isinstance(result, (list, tuple)) else [result]
    ids = set()
    for ref in refs:
        task_id = getattr(ref, "task_id", None)
        if not callable(task_id):
            continue
        ids.add(task_id().hex())
    return ids


def _patch_constructor(module):
    cls = getattr(module, "ActorClass", None)
    original = getattr(cls, "_remote", None)
    if not callable(original):
        _warn("unsupported_actor_creation_hook", "Ray has no compatible ActorClass._remote hook")
        return
    if getattr(original, "__callnav_ray__", False):
        return

    @functools.wraps(original)
    def create(self, *args, **kwargs):
        captured = None
        # get_if_exists may return a handle without submitting any constructor.
        # Ray's create path recursively calls _remote(get_if_exists=False); only
        # that actual creation is eligible for a submission receipt.
        if not kwargs.get("get_if_exists", False):
            try:
                frame = _source_frame()
                if frame is not None:
                    captured = _submission_site(frame, "__init__")
            except Exception as exc:
                _warn("creation_location_failed", str(exc))
        result = original(self, *args, **kwargs)
        if captured is not None:
            try:
                from ray._raylet import TaskID

                actor_id = result._ray_actor_id
                caller, sources = captured
                _event({
                    "type": "submission",
                    "task_id": TaskID.for_actor_creation_task(actor_id).hex(),
                    "actor_id": actor_id.hex(), "method": "__init__",
                    "caller": caller, "sources": sources,
                })
            except Exception as exc:
                _warn("creation_receipt_failed", str(exc))
        return result

    create.__callnav_ray__ = True
    cls._remote = create


def _patch_actor(module):
    _patch_constructor(module)
    cls = getattr(module, "ActorHandle", None)
    original = getattr(cls, "_actor_method_call", None)
    if not callable(original):
        _warn("unsupported_actor_hook", "Ray has no compatible ActorHandle._actor_method_call hook")
        return
    if getattr(original, "__callnav_ray__", False):
        return

    @functools.wraps(original)
    def submit(self, method_name, *args, **kwargs):
        captured = None
        if not method_name.startswith("__ray"):
            try:
                frame = _source_frame()
                if frame is not None:
                    captured = _submission_site(frame, method_name)
            except Exception as exc:
                _warn("submission_location_failed", str(exc))
        result = original(self, method_name, *args, **kwargs)
        if captured is not None:
            try:
                caller, sources = captured
                task_ids = _task_ids(result)
                if not task_ids:
                    _warn("unsupported_return", "Ray zero-return or streaming actor tasks cannot yet be correlated")
                for task_id in task_ids:
                    _event({
                        "type": "submission", "task_id": task_id,
                        "actor_id": self._ray_actor_id.hex(), "method": method_name,
                        "caller": caller, "sources": sources,
                    })
            except Exception as exc:
                _warn("submission_receipt_failed", str(exc))
        return result

    submit.__callnav_ray__ = True
    cls._actor_method_call = submit


def _method_codes(method):
    codes = {}
    seen = set()
    while (inspect.isfunction(method) or inspect.ismethod(method)) and id(method) not in seen:
        seen.add(id(method))
        code = getattr(method, "__code__", None)
        if code is not None:
            codes.setdefault(code, method.__func__ if inspect.ismethod(method) else method)
        method = getattr(method, "__wrapped__", None)
    return codes


def _execution_context(method_name, codes):
    import ray

    context = ray.get_runtime_context()
    return {
        "task_id": context.get_task_id(), "actor_id": context.get_actor_id(),
        "method": method_name, "codes": codes, "root_frame": None, "receipts": {},
    }


def observe_entry(frame):
    """Confirm actual entries, following only a proven, still-active wrapper chain."""
    pending = _pending.get()
    if not pending:
        return
    try:
        root = pending["root_frame"]
        if root is None:
            if frame.f_code not in pending["codes"]:
                return
            pending["root_frame"] = frame
            _collector.bind_function(frame, pending["codes"][frame.f_code])
            wrappers = []
        else:
            # Coroutine resumes produce another call event for the same frame.
            # Keep the deepest body already observed rather than resetting it.
            if frame is root:
                return
            wrappers = []
            child = frame
            for _ in range(8):
                parent = child.f_back
                if parent is None or not _collector.wrapper_link(parent, child):
                    return
                wrappers.append(parent)
                if parent is root:
                    break
                child = parent
            else:
                return
            wrappers.reverse()
        path = _collector.path(frame.f_code.co_filename)
        if path is None or not pending["task_id"] or not pending["actor_id"]:
            return
        source = _collector.source(path)
        if source is None:
            return
        sources = {path: {"sha256": source.sha256}}
        via = []
        for wrapper in wrappers:
            wrapper_path = os.path.realpath(wrapper.f_code.co_filename)
            wrapper_source = _collector.source(wrapper_path)
            if wrapper_source is None:
                return
            sources[wrapper_path] = {"sha256": wrapper_source.sha256}
            via.append(dict(wrapper_source.target(wrapper.f_code), sha256=wrapper_source.sha256))
        path_key = tuple((id(item), item.f_code) for item in [*wrappers, frame])
        receipts = pending["receipts"]
        # Replace an ancestor only after its actual forwarded child entered.
        # Independent sibling bodies remain separate, observed candidates.
        for previous in list(receipts):
            if len(previous) < len(path_key) and path_key[:len(previous)] == previous:
                del receipts[previous]
        receipts[path_key] = {
            "type": "execution", "task_id": pending["task_id"],
            "actor_id": pending["actor_id"], "method": pending["method"],
            "target": source.target(frame.f_code), "sources": sources, "via": via,
        }
    except Exception as exc:
        _warn("execution_receipt_failed", str(exc))


def _finish_execution(token):
    pending = _pending.get()
    _pending.reset(token)
    if pending is not None:
        # A frame is needed only while the actual method is executing. Never
        # retain user frames in receipts, the context, or the process index.
        pending["root_frame"] = None
        for receipt in pending.pop("receipts", {}).values():
            _event(receipt)


def _patch_manager(module):
    cls = getattr(module, "FunctionActorManager", None)
    original = getattr(cls, "_make_actor_method_executor", None)
    if not callable(original):
        _warn("unsupported_execution_hook", "Ray has no compatible FunctionActorManager execution hook")
        return
    if getattr(original, "__callnav_ray__", False):
        return

    @functools.wraps(original)
    def make_executor(self, method_name, method):
        executor = original(self, method_name, method)
        codes = _method_codes(method)
        if not codes:
            return executor
        if inspect.isgeneratorfunction(method) or inspect.isasyncgenfunction(method):
            _warn("generator_actor", "Generator actor method bodies are not yet correlated")
            return executor
        if inspect.iscoroutinefunction(method):
            @functools.wraps(executor)
            async def tracked(*args, **kwargs):
                token = None
                try:
                    token = _pending.set(_execution_context(method_name, codes))
                except Exception as exc:
                    _warn("execution_context_failed", str(exc))
                try:
                    return await executor(*args, **kwargs)
                finally:
                    if token is not None:
                        _finish_execution(token)
        else:
            @functools.wraps(executor)
            def tracked(*args, **kwargs):
                token = None
                try:
                    token = _pending.set(_execution_context(method_name, codes))
                except Exception as exc:
                    _warn("execution_context_failed", str(exc))
                try:
                    return executor(*args, **kwargs)
                finally:
                    if token is not None:
                        _finish_execution(token)
        return tracked

    make_executor.__callnav_ray__ = True
    cls._make_actor_method_executor = make_executor


def _patch(module):
    if module.__name__ == "ray":
        _patch_ray(module)
    elif module.__name__ == "ray.actor":
        _patch_actor(module)
    elif module.__name__ == "ray._private.function_manager":
        _patch_manager(module)


class _Loader(importlib.abc.Loader):
    def __init__(self, wrapped):
        self.wrapped = wrapped

    def create_module(self, spec):
        method = getattr(self.wrapped, "create_module", None)
        return method(spec) if method else None

    def exec_module(self, module):
        self.wrapped.exec_module(module)
        _patch(module)

    def __getattr__(self, name):
        return getattr(self.wrapped, name)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _MODULES:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and spec.loader and not isinstance(spec.loader, _Loader):
            spec.loader = _Loader(spec.loader)
        return spec


def install(collector):
    """Install without importing Ray, before the application's ray.init()."""
    global _collector, _finder
    _collector = collector
    collector.entry_observer = observe_entry
    if _finder is None:
        _finder = _Finder()
        sys.meta_path.insert(0, _finder)
    for name in ("ray.actor", "ray._private.function_manager", "ray"):
        module = sys.modules.get(name)
        if module is not None and not getattr(getattr(module, "__spec__", None), "_initializing", False):
            _patch(module)


def merge_events(index, run_dir):
    """Join submission and actual-entry receipts; unmatched work never navigates."""
    submissions = {}
    executions = {}
    diagnostics = []
    versions = set()
    malformed = 0
    for path in sorted((Path(run_dir) / "ray").glob("*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("Ray event is not an object")
                    if event.get("run_id") != index["run_id"]:
                        continue
                    sources = event.get("sources", {})
                    if not isinstance(sources, dict):
                        raise ValueError("Ray event sources is not an object")
                    for source_path, source in sources.items():
                        if not isinstance(source_path, str) or not isinstance(source, dict) or not isinstance(source.get("sha256"), str):
                            raise ValueError("invalid Ray source metadata")
                    kind = event.get("type")
                    if kind in ("submission", "execution"):
                        for field in ("task_id", "actor_id", "method"):
                            if not isinstance(event.get(field), str) or not event[field]:
                                raise ValueError("invalid Ray task identity")
                        if not isinstance(event.get("pid"), int):
                            raise ValueError("invalid Ray process ID")
                        location = event.get("caller" if kind == "submission" else "target")
                        if not isinstance(location, dict) or not isinstance(location.get("path"), str) or not isinstance(location.get("line"), int):
                            raise ValueError("invalid Ray source location")
                        via = event.get("via", [])
                        if not isinstance(via, list) or any(
                            not isinstance(item, dict) or not isinstance(item.get("path"), str)
                            or not isinstance(item.get("line"), int) or not isinstance(item.get("sha256"), str)
                            for item in via
                        ):
                            raise ValueError("invalid Ray wrapper evidence")
                    for source_path, source in sources.items():
                        previous = index["sources"].get(source_path)
                        if previous and previous["sha256"] != source["sha256"]:
                            index["sources"][source_path] = {"sha256": "", "conflicting": True}
                        elif previous is None:
                            index["sources"][source_path] = source
                    if kind == "installed":
                        versions.add(event["version"])
                    elif kind == "diagnostic":
                        diagnostics.append({"code": event["code"], "message": event["message"], "pid": event["pid"]})
                    elif kind in ("submission", "execution"):
                        key = (event["task_id"], event["actor_id"], event["method"])
                        if kind == "submission":
                            submissions[key] = event
                        else:
                            executions.setdefault(key, []).append(event)
                except (ValueError, KeyError, TypeError):
                    malformed += 1
        except OSError as exc:
            index["warnings"].append(f"Unreadable Ray event file {path.name}: {exc}")
    edges = {}
    matched = 0
    for key, submitted in submissions.items():
        receipts = executions.get(key, [])
        if not receipts:
            continue
        matched += 1
        # Retries can produce several worker entries for the same logical task.
        targets = {}
        for receipt in receipts:
            target_key = json.dumps([receipt["target"], receipt.get("via", [])], sort_keys=True)
            targets.setdefault(target_key, []).append(receipt)
        for target_receipts in targets.values():
            target = target_receipts[0]["target"]
            via = target_receipts[0].get("via", [])
            edge_key = json.dumps([submitted["caller"], target, via], sort_keys=True)
            edge = edges.setdefault(edge_key, {
                "caller": submitted["caller"], "target": target, "kind": "ray_actor", "count": 0,
                "process_ids": [], "ray": {"worker_confirmed": True, "task_ids": [], "actor_ids": [], "worker_process_ids": []},
            })
            if via:
                edge["via"] = via
            edge["count"] += 1
            edge["process_ids"] = sorted(set(edge["process_ids"] + [submitted["pid"]] + [r["pid"] for r in target_receipts]))
            edge["ray"]["task_ids"].append(key[0])
            edge["ray"]["actor_ids"] = sorted(set(edge["ray"]["actor_ids"] + [key[1]]))
            edge["ray"]["worker_process_ids"] = sorted(set(edge["ray"]["worker_process_ids"] + [r["pid"] for r in target_receipts]))
    index["edges"].extend(edges.values())
    unmatched = len(submissions) - matched
    index["ray"] = {
        "versions": sorted(versions), "submissions": len(submissions),
        "execution_tasks": len(executions), "matched_tasks": matched,
        "unmatched_submissions": unmatched,
        "unmatched_executions": len(set(executions) - set(submissions)),
        "malformed_event_lines": malformed, "diagnostics": diagnostics,
    }
    if unmatched:
        index["warnings"].append(f"{unmatched} Ray actor submissions have no confirmed worker entry; no navigation edge was inferred for them.")
    if malformed:
        index["warnings"].append(f"Ignored {malformed} incomplete or malformed Ray event lines.")
    return index
