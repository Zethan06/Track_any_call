"""Keep an opt-in recording environment when a shell submits a local Ray job.

The recorder places a tiny ``ray`` shim first on PATH.  Other Ray commands are
executed unchanged; only blocking ``ray job submit -- ...`` is adapted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from .slime import _merge_pythonpath


class RayCLIError(ValueError):
    """A Ray submission cannot safely retain this recording session."""


def merge_submit_args(argv: Sequence[str], env: Mapping[str, str]) -> list[str]:
    """Copy an argv vector and merge recording variables into a JSON runtime env.

    No shell text is parsed or reconstructed.  The application arguments after
    ``--`` are retained byte-for-byte as Python strings.
    """
    result = list(argv)
    if result[:2] != ["job", "submit"]:
        return result
    try:
        separator = result.index("--", 2)
    except ValueError as exc:
        raise RayCLIError("recorded Ray submissions require `ray job submit [options] -- COMMAND`") from exc
    options = result[2:separator]
    runtime_index: int | None = None
    runtime_value: str | None = None
    equals_form = False
    for index, option in enumerate(options, start=2):
        if option == "--no-wait" or option.startswith("--no-wait="):
            raise RayCLIError("--no-wait is unsupported: recording must wait for the submitted job to finish")
        if option == "--runtime-env" or option.startswith("--runtime-env="):
            raise RayCLIError("runtime-env YAML files are unsupported; use --runtime-env-json for recording")
        if option == "--runtime-env-json" or option.startswith("--runtime-env-json="):
            if runtime_index is not None:
                raise RayCLIError("multiple --runtime-env-json options are unsupported")
            runtime_index = index
            equals_form = option.startswith("--runtime-env-json=")
            if equals_form:
                runtime_value = option.split("=", 1)[1]
            elif index + 1 < separator:
                runtime_value = result[index + 1]
            else:
                raise RayCLIError("--runtime-env-json requires a JSON value")
    runtime: Any = {}
    if runtime_value is not None:
        try:
            runtime = json.loads(runtime_value)
        except json.JSONDecodeError as exc:
            raise RayCLIError(f"invalid --runtime-env-json: {exc.msg}") from exc
    if not isinstance(runtime, dict):
        raise RayCLIError("--runtime-env-json must contain an object")
    provided = runtime.get("env_vars", {})
    if not isinstance(provided, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in provided.items()):
        raise RayCLIError("runtime env_vars must map strings to strings")
    merged = dict(provided)
    merged.update({name: value for name, value in env.items() if name.startswith("CALLNAV_")})
    merged["PYTHONPATH"] = _merge_pythonpath(env.get("PYTHONPATH", ""), merged.get("PYTHONPATH", ""))
    runtime["env_vars"] = merged
    encoded = json.dumps(runtime, separators=(",", ":"))
    if runtime_index is None:
        result[separator:separator] = ["--runtime-env-json", encoded]
    elif equals_form:
        result[runtime_index] = "--runtime-env-json=" + encoded
    else:
        result[runtime_index + 1] = encoded
    return result


def daemon_environment(env: Mapping[str, str]) -> dict[str, str]:
    """Remove this recording's opt-in before starting long-lived Ray daemons."""
    cleaned = {name: value for name, value in env.items() if not name.startswith("CALLNAV_")}
    bootstrap = env.get("CALLNAV_BOOTSTRAP_PATH", "")
    if bootstrap:
        # A launcher may prepend its own tool/module directories. Remove our
        # injected entries wherever they moved, preserving the user's entries.
        pythonpath = [entry for entry in env.get("PYTHONPATH", "").split(os.pathsep)
                      if entry != bootstrap]
        package_root = str(Path(__file__).resolve().parents[2])
        if package_root in pythonpath:
            pythonpath.remove(package_root)
        if pythonpath:
            cleaned["PYTHONPATH"] = os.pathsep.join(pythonpath)
        else:
            cleaned.pop("PYTHONPATH", None)
        shim_directory = str(Path(bootstrap) / "bin")
        cleaned["PATH"] = os.pathsep.join(
            entry for entry in env.get("PATH", "").split(os.pathsep)
            if entry != shim_directory
        )
    return cleaned


def main(argv: Sequence[str] | None = None) -> None:
    """Replace this shim with the original executable, retaining exit/signals."""
    real = os.environ.get("CALLNAV_REAL_RAY", "")
    if not real or not os.path.isabs(real):
        raise SystemExit("callnav: CALLNAV_REAL_RAY must name the original absolute Ray executable")
    try:
        args = merge_submit_args(sys.argv[1:] if argv is None else argv, os.environ)
    except RayCLIError as exc:
        print(f"callnav: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if args[:1] == ["start"]:
        os.execve(real, [real, *args], daemon_environment(os.environ))
        return
    os.execv(real, [real, *args])


if __name__ == "__main__":
    main()
