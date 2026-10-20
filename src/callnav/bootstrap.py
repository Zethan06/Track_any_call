"""Opt-in startup hook used by the record command and inherited children."""

import importlib
import os
import sys

_active = False


def activate():
    global _active
    if _active or not os.environ.get("CALLNAV_RUN_DIR"):
        return
    _active = True
    # Adapter failure must terminate startup: site.py otherwise prints and
    # suppresses ordinary sitecustomize exceptions, potentially running a full job.
    try:
        if sys.version_info < (3, 11):
            raise RuntimeError(
                "recording requires Python 3.11+; use a compatible interpreter in your launch command"
            )
        adapter = os.environ.get("CALLNAV_ADAPTER")
        if adapter:
            if adapter != "slime":
                raise ValueError("unsupported adapter: " + adapter)
            importlib.import_module("callnav.adapters.slime").install()
        from .collector import start

        collector = start()
        if os.environ.get("CALLNAV_RAY") == "1":
            from .adapters import ray

            ray.install(collector)
    except BaseException as exc:
        print("callnav: startup failed: " + str(exc), file=sys.stderr)
        raise SystemExit(86) from exc


def chain_sitecustomize(bootstrap_directory):
    """Execute the customization hidden by our temporary sitecustomize module."""
    current = sys.modules.get("sitecustomize")
    directory = os.path.realpath(bootstrap_directory)
    removed = [(index, path) for index, path in enumerate(sys.path)
               if os.path.realpath(path or os.getcwd()) == directory]
    sys.path[:] = [p for p in sys.path if os.path.realpath(p or os.getcwd()) != directory]
    sys.modules.pop("sitecustomize", None)
    try:
        try:
            importlib.import_module("sitecustomize")
        except ModuleNotFoundError as exc:
            if exc.name != "sitecustomize":
                raise
    except BaseException as exc:
        print("callnav: existing sitecustomize failed: " + str(exc), file=sys.stderr)
        raise SystemExit(86) from exc
    finally:
        # Preserve changes made by the user's customization (e.g. package paths).
        for index, path in removed:
            if path not in sys.path:
                sys.path.insert(min(index, len(sys.path)), path)
        if "sitecustomize" not in sys.modules and current is not None:
            sys.modules["sitecustomize"] = current

