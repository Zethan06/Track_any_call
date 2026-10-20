"""Record a real command and merge its per-process call edges."""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import __version__


def _write_json(path, value):
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def merge(run_dir):
    """Merge already flushed shards. A later merge includes late worker writes."""
    run_dir = Path(run_dir).resolve()
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    sources = {}
    processes = []
    edges = {}
    warnings = []
    for path in sorted((run_dir / "shards").glob("*.json")):
        try:
            shard = json.loads(path.read_text(encoding="utf-8"))
            if shard["run_id"] != manifest["run_id"]:
                warnings.append("ignored foreign shard: " + path.name)
                continue
            process = shard["process"]
            processes.append(process)
            for source_path, source in shard["sources"].items():
                previous = sources.get(source_path)
                if previous and previous["sha256"] != source["sha256"]:
                    sources[source_path] = {"sha256": "", "conflicting": True}
                elif not previous:
                    sources[source_path] = source
            for edge in shard["edges"]:
                # Offsets distinguish call expressions even on Python 3.10.
                key = json.dumps(
                    [edge["caller"], edge["target"], edge["kind"], edge.get("via", [])], sort_keys=True
                )
                if key not in edges:
                    edges[key] = dict(edge, process_ids=list(edge["process_ids"]))
                else:
                    edges[key]["count"] += edge["count"]
                    if "wrapper_forwarded_count" in edge:
                        edges[key]["wrapper_forwarded_count"] = (
                            edges[key].get("wrapper_forwarded_count", 0) + edge["wrapper_forwarded_count"]
                        )
                    edges[key]["process_ids"] = sorted(set(edges[key]["process_ids"] + edge["process_ids"]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append("unreadable shard " + path.name + ": " + str(exc))
    if not processes:
        warnings.append("No Python process reported coverage. Check PYTHONPATH, -S/-I, and remote runtime environments.")
    if any(process["status"] != "complete" for process in processes):
        warnings.append("Some processes did not flush a clean completion; their edges may be partial.")
    adapter_markers = []
    for path in sorted((run_dir / "adapter").glob("*.json")):
        try:
            adapter_markers.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            warnings.append("unreadable adapter marker " + path.name + ": " + str(exc))
    index = {
        "schema_version": 1, "run_id": manifest["run_id"], "root": manifest["root"],
        "roots": manifest.get("roots", [manifest["root"]]),
        "sources": sources, "processes": processes, "edges": list(edges.values()),
        "coverage": "reported_python_processes_only", "warnings": warnings,
        "limitations": [
            "Only executed Python calls with both endpoints under the configured source roots are recorded.",
            "Only proven immediate await fn(...) coroutine entries are mapped; deferred awaits, resumes and generators are excluded.",
            "C/native calls and HTTP causal edges are not inferred.",
            "Ray calls require --ray and matching task IDs from instrumented submitter and worker processes.",
            "Observed native-to-Python callbacks are marked ambiguous and excluded from source navigation.",
            "Callee expressions that cannot be resolved without running user code are conservatively marked ambiguous.",
            "Ambiguous source ranges are excluded from runtime navigation.",
            "Existing threads, environment-clearing children, python -S/-I, and external workers may be uninstrumented.",
        ],
    }
    if manifest.get("adapter"):
        index["adapter"] = {
            "name": manifest["adapter"],
            "status": "configured" if adapter_markers else "not_observed",
            "processes": adapter_markers,
        }
        if not adapter_markers:
            warnings.append("Framework iteration budget was not observed; verify the supported entrypoint was reached.")
    if manifest.get("ray"):
        from .adapters.ray import merge_events

        merge_events(index, run_dir)
    _write_json(run_dir / "index.json", index)
    latest = Path(index["root"]) / ".callnav" / "latest.json"
    if latest.exists():
        try:
            if json.loads(latest.read_text(encoding="utf-8")).get("run_id") == index["run_id"]:
                _write_json(latest, index)
        except (OSError, ValueError):
            pass
    return index


def record(args, parser):
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("record requires a command after --")
    if args.iterations is not None and not args.adapter:
        parser.error("--iterations requires a framework adapter; arbitrary scripts have no generic step boundary")
    if args.iterations is not None and args.iterations < 1:
        parser.error("--iterations must be positive")
    root = Path(args.root).resolve()
    if not root.is_dir():
        parser.error("--root must be an existing source directory")
    roots = [str(root)]
    for included in args.include_root:
        directory = Path(included).resolve()
        if not directory.is_dir():
            parser.error("--include-root must be an existing source directory: " + str(directory))
        if str(directory) not in roots:
            roots.append(str(directory))
    run_id = str(uuid.uuid4())
    output = Path(args.output or (root / ".callnav" / run_id)).resolve()
    if output.exists() and any(output.iterdir()):
        parser.error("--output must be empty or new; each run has its own directory")
    output.mkdir(parents=True, exist_ok=True)
    bootstrap = output / ".bootstrap"
    bootstrap.mkdir()
    package_root = str(Path(__file__).resolve().parent.parent)
    (bootstrap / "sitecustomize.py").write_text(
        "import os, sys\n"
        + "sys.path.insert(0, " + repr(package_root) + ")\n"
        + "from callnav.bootstrap import chain_sitecustomize, activate\n"
        + "chain_sitecustomize(os.path.dirname(__file__))\n"
        + "activate()\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(CALLNAV_RUN_DIR=str(output), CALLNAV_ROOT=str(root), CALLNAV_RUN_ID=run_id,
               CALLNAV_INCLUDE_ROOTS=json.dumps(roots[1:]))
    env.pop("CALLNAV_ADAPTER", None)
    env.pop("CALLNAV_ITERATIONS", None)
    env.pop("CALLNAV_RAY", None)
    if args.ray:
        env["CALLNAV_RAY"] = "1"
    if args.adapter:
        env["CALLNAV_ADAPTER"] = args.adapter
        env["CALLNAV_ITERATIONS"] = str(args.iterations or 1)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(bootstrap), package_root] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env["CALLNAV_BOOTSTRAP_PATH"] = str(bootstrap)
    if args.ray or args.adapter == "slime":
        real_ray = shutil.which("ray")
        if real_ray:
            binary_dir = bootstrap / "bin"
            binary_dir.mkdir()
            shim = binary_dir / "ray"
            shim.write_text(
                "#!/bin/sh\nexec " + shlex.quote(sys.executable)
                + ' -m callnav.adapters.ray_cli "$@"\n',
                encoding="utf-8",
            )
            shim.chmod(0o755)
            env["CALLNAV_REAL_RAY"] = os.path.abspath(real_ray)
            env["PATH"] = str(binary_dir) + os.pathsep + env.get("PATH", "")
    manifest = {
        "schema_version": 1, "run_id": run_id, "root": str(root), "roots": roots,
        "command": command, "started_at": time.time(), "status": "running",
        "adapter": args.adapter, "iterations": args.iterations, "ray": args.ray,
    }
    _write_json(output / "run.json", manifest)
    print("callnav: recording to " + str(output), file=sys.stderr)
    try:
        result = subprocess.run(command, env=env, check=False)
        returncode = result.returncode
    except KeyboardInterrupt:
        returncode = 130
    except OSError as exc:
        print("callnav: cannot start command: " + str(exc), file=sys.stderr)
        returncode = 127
    finally:
        manifest.update(status="finished", finished_at=time.time())
        manifest["exit_code"] = locals().get("returncode", 130)
        _write_json(output / "run.json", manifest)
        index = merge(output)
        latest = root / ".callnav" / "latest.json"
        latest.parent.mkdir(parents=True, exist_ok=True)
        _write_json(latest, index)
        print("callnav: " + str(len(index["edges"])) + " edges, " + str(len(index["processes"]))
              + " reported Python processes; " + str(output / "index.json"), file=sys.stderr)
        for warning in index["warnings"]:
            print("callnav: warning: " + warning, file=sys.stderr)
    return returncode if returncode >= 0 else 128 - returncode


def main(argv=None):
    parser = argparse.ArgumentParser(prog="callnav", description=__doc__)
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    rec = subparsers.add_parser("record", help="record an existing Python or shell launch command")
    rec.add_argument("--root", default=".", help="primary source tree; also owns .callnav/latest.json")
    rec.add_argument("--include-root", action="append", default=[], metavar="PATH",
                     help="additional source tree to collect; repeat for external dependencies")
    rec.add_argument("--output", help="new or empty run output directory")
    rec.add_argument("--adapter", choices=["slime"])
    rec.add_argument("--ray", action="store_true", help="connect Ray submissions to methods observed executing in instrumented workers")
    rec.add_argument("--iterations", type=int, help="complete outer iterations; requires --adapter")
    rec.add_argument("command", nargs=argparse.REMAINDER)
    mer = subparsers.add_parser("merge", help="refresh index.json from flushed process shards")
    mer.add_argument("run_dir")
    args = parser.parse_args(argv)
    if args.action == "merge":
        index = merge(args.run_dir)
        print(str(Path(args.run_dir).resolve() / "index.json") + ": " + str(len(index["edges"])) + " edges")
        return 0
    return record(args, parser)
