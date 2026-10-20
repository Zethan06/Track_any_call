# Usage guide

The normal workflow is: install the recorder in a suitable Python environment, wrap your existing launch command, then open its index in VS Code.

## Install the recorder and extension

Obtain the CallNav source checkout. These instructions install from source and do not assume a published PyPI or Marketplace package.

Python 3.11+ is required for the recorder and every Python process you want to record. Its instruction-level source positions distinguish calls on the same line. A child interpreter older than 3.11 is rejected during recording startup.

For an existing application, activate its normal environment and install the checkout:

```bash
python -m pip install /path/to/callnav
callnav --version
callnav --help
```

CallNav does not install your application's dependencies, Ray, CUDA, or an ML framework.

You may instead keep the CLI in a separate virtual environment and invoke its executable by path. The Python processes started by your command still need Python 3.11+ and access to CallNav's installed package and bootstrap paths. Activating your application's environment before recording is usually easier.

Build the extension from the CallNav checkout:

```bash
python vscode/scripts/package.py
```

Install the printed file with **Extensions: Install from VSIX…**. The packager needs Python, with no npm dependencies. VS Code 1.85+ is required. For Remote-SSH, install into the remote workspace rather than only the local UI.

## Wrap your existing command

Change to the working directory your script expects:

```bash
cd /path/to/your-project
callnav record --root "$PWD" -- bash ./launch.sh --config ./configs/debug.yaml
```

Or launch Python directly:

```bash
callnav record --root "$PWD" -- python train.py --steps 1
callnav record --root "$PWD" -- python -m your_package --config ./config.yaml
```

The `--steps 1` option above is an example of an application's option, not a CallNav flag. Everything after `--` is passed as the command's argument vector. Recording does not change the working directory. It preserves normal terminal output and returns the command's exit status.

Use the same Python executable, environment variables, model/data settings, and launch arguments your application normally needs. If a script selects another interpreter or activates another environment, that interpreter must support CallNav and be able to read the injected package/bootstrap paths. Start with a small representative workload: initialization, rollout, training, evaluation, and cleanup only happen if your command executes them.

CallNav does not modify a generic script to stop after one step. Use the framework's own limits; the optional synchronous Slime adapter below has a specific supported iteration boundary.

## Recorder options

```text
callnav record [--root PATH] [--include-root PATH ...] [--output PATH]
               [--ray] [--adapter slime] [--iterations N]
               -- COMMAND [ARG ...]
callnav merge RUN_DIRECTORY
```

| Option | Meaning |
| --- | --- |
| `--root PATH` | Primary source tree; defaults to the current directory. Owns `.callnav/latest.json`. |
| `--include-root PATH` | Additional source tree, repeatable. Both endpoints of an ordinary call must be within configured trees. |
| `--output PATH` | New or empty recording directory; existing nonempty output is rejected. |
| `--ray` | Enable supported Ray Actor matching and the PATH-based Ray Job CLI environment shim. |
| `--adapter slime` | Enable the experimental synchronous Slime integration, including its Ray CLI environment shim. |
| `--iterations N` | Complete outer Slime iterations; requires the adapter and a positive count. Default with the adapter is 1. |

Paths are resolved from the recorder's current working directory. Source roots select collection scope; they do not install packages or change Python's import resolution.

Include a sibling library checkout as needed:

```bash
callnav record --root "$PWD" \
  --include-root ../library-a \
  --include-root ../library-b \
  -- bash ./launch.sh
```

For an installed Python dependency, pass the actual source directory you intend to inspect. Adding a large environment directory increases collection scope and overhead; prefer specific packages or checkouts.

## Output and navigation

A normal run creates:

```text
your-project/
  .callnav/
    latest.json             # latest merged index
    <run-id>/
      run.json              # launch metadata and exit status
      index.json            # merged navigation index
      shards/               # per-process reports
      .bootstrap/           # startup hook for this run
```

Optional backends add their own event/adapter files. The terminal prints the exact output directory and any coverage warnings. If you use `--output`, the latest convenience index still belongs to the primary source root.

In VS Code:

1. Open the primary source folder.
2. Run **CallNav: Load Latest Runtime Index**.
3. Run **CallNav: Enable Runtime Navigation**.
4. Ctrl/Cmd+click an observed call, or use **Go to Definition**.

Use **CallNav: Load Runtime Index…** to select a particular historical `index.json`. After another run or merge, reload the index to use its updated contents. In a multi-root workspace, loading the latest index uses the active document's workspace folder; you can select a file explicitly if needed.

CallNav adds definitions to ordinary Python documents. Static providers remain active, including for calls the recording did not observe. Multiple observed implementations and static definitions may appear in VS Code's native chooser.

The extension verifies recorded source fingerprints. Save or restore matching source if a file is dirty or changed; record again after changing code. The current format uses absolute paths from your own runtime. It has no path remapping: when changing checkout location, container, or host, record again there.

Add `.callnav/` to your application's `.gitignore`. Recordings contain source paths, command-line arguments, and process arguments. They do not serialize function argument or return values, but launch arguments may still contain sensitive data.

## Remote-SSH

1. Put the CallNav checkout and application on the machine that runs the workload.
2. Install the recorder in the application's environment there.
3. Record from that application's working directory.
4. Open the same remote source folder with VS Code Remote-SSH.
5. Install the VSIX in the remote workspace and load its index.

The extension is a workspace extension. It reads source files and the index on that host; no browser UI or separate CallNav service is needed. Recording on a remote host and opening a local copy at another path does not work through an index setting alone.

## Ray and framework launchers

### Ray Actor calls

```bash
callnav record --ray --root "$PWD" -- python ray_main.py
```

The experimental backend was developed against **Ray 2.58** and uses Ray internals. Other versions need their own validation. Your existing environment must provide Ray; CallNav does not depend on or install it.

The backend connects supported Actor method submissions and constructors to worker entries using task/actor IDs. It supports synchronous and async Actor methods, inherited Python constructors, and observed decorator forwarding chains. Fetching an existing named Actor does not create a constructor edge. Calls without matching execution evidence do not become definite runtime destinations.

Both submitters and workers must start with collection enabled. Bootstrap, package, source, and recording directories must be accessible at the same paths. The adapter merges recording environment variables into observed `ray.init(runtime_env=...)` calls while preserving existing settings. This is not a multi-host deployment service.

Activate the final environment that provides your intended `ray` executable **before** running `callnav record`. The recorder captures that executable and places its temporary shim first on PATH. A later `conda activate` or `export PATH=...` inside the launch script can bypass the shim, or leave it using the Ray executable captured before the environment changed. Move that environment setup before recording.

For PATH-based `ray job submit [options] -- command`, `--ray` supplies a temporary CLI shim that merges recording variables and the injected `PYTHONPATH` into `--runtime-env-json`. Existing environment settings and command arguments are preserved. A launcher can therefore keep its ordinary blocking job submission:

```bash
callnav record --ray --root "$PWD" -- bash ./ray-launch.sh
```

The shim rejects `--no-wait` and `--runtime-env` files: it must wait for the job, and currently supports JSON runtime environments only. An absolute `ray` executable bypasses the PATH shim. When a script starts Ray daemons with `ray start`, the shim removes recording opt-in variables from those long-lived daemons; the submitted job receives its own explicit recording environment. Containers, external services, and launchers replacing environments still require explicit integration. Inspect reported processes and warnings rather than assuming all workers were captured.

Regular Ray remote functions, generator Actor methods, pre-existing uninstrumented workers, and HTTP causal links are outside this backend's supported matching scope.

### Frameworks such as VERL and asynchronous Slime

Use your normal launch command with `--ray` where appropriate, keep its existing framework-specific step limits, and include external source trees you want to navigate. No generic framework integration is required for ordinary instrumented Python calls.

A full asynchronous training script still needs its original environment, workers, data, and resource configuration. Recording does not provision those. Ray Job launchers must use the supported submission form and satisfy the shared-path requirements above.

### Optional synchronous Slime adapter

```bash
callnav record --ray --root "$PWD" --adapter slime --iterations 1 \
  -- bash ./launch.sh
```

This adapter targets a particular synchronous `train.py` structure: model initialization through `create_training_models`, followed by `range(args.start_rollout_id, args.num_rollout)`. It sets the stop index after the resume start is discovered, so resuming at iteration 8 with a budget of 1 runs iteration 8 once.

Initialization, rollout, reward, training microsteps, weight synchronization, configured evaluation/checkpoint behavior, and normal cleanup remain part of the application. The original script's normal side effects still occur.

The adapter uses the same PATH-based Ray CLI environment shim as `--ray`. It also patches the supported Slime actor environment helper to carry collection into new workers. Use `--ray` as shown above when you want Actor causal edges in addition to per-process Python calls.

This adapter does **not** support `train_async.py`, evaluation-only mode, `--no-wait`, or `--runtime-env` files. Absolute `ray` executables bypass the shim. Renamed/custom entry points and changed Slime APIs require validation. Its iteration limiting is separate from ordinary recording of an async script.

## Coverage and limitations

CallNav records Python call relationships observed under the configured source roots. It does not prove complete coverage of a project or all execution processes.

| Area | Current behavior |
| --- | --- |
| Dynamic Python calls | Records observed concrete implementations, including many registry and subclass selections. |
| Threads and child processes | New threads, normally inherited Python subprocesses, and multiprocessing spawn can participate. Existing threads/workers or cleared startup environments may not. |
| Python versions | Python 3.11+ is required for the CLI and participating processes; older child interpreters are rejected at startup. |
| `functools.partial` | Exact built-in partial objects can resolve to their observed Python function. Arbitrary callable subclasses are not assumed equivalent. |
| Decorators | Follows bounded, actually executed forwarding chains. `__wrapped__` alone is not proof. Conditional nonforwarding invocations retain wrapper candidates. |
| Async | Supported immediate `await fn(...)` maps the initial coroutine entry. Deferred creation/scheduling, resumes, and generators are not reconstructed as equivalent caller edges. Ordinary synchronous calls inside them may still be recorded. |
| Native code | C/CUDA/JIT internals and native/HTTP causal relationships are not reconstructed. Native callbacks without a safe call-site proof remain ambiguous. |
| Unresolvable expressions | Ambiguous or unproven call relationships are skipped for runtime navigation. |
| Other profilers | Another tool can replace `sys.setprofile`; inspect process reports for conflicts. |
| Isolated startup | `python -S`, `python -I`, cleared environments, and inaccessible bootstrap paths can bypass collection. |

Profile-based collection adds overhead. Start with a small run, narrow the source roots, and measure overhead on your workload before a long recording.

Processes normally report at shutdown and periodically flush. A process killed with `SIGKILL`, `os._exit`, or a worker still running when the launcher exits may leave partial data. The index lists **reported** processes and diagnostics; an absent process is not evidence that it was recorded.

After late workers have flushed, refresh the index:

```bash
callnav merge .callnav/<run-id>
```

Use the actual directory printed by the recorder instead of the placeholder. Merge reads existing shards; it cannot recover events that were never flushed. It refreshes `latest.json` only when that file still refers to the same run, so merging an older run does not replace a newer latest index.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `callnav` is not found | Activate the environment where you installed it, use its executable path, or run `python -m callnav`. |
| No Python process reported | Check interpreter versions, `PYTHONPATH`, `-S/-I`, environment clearing, shared paths, and job runtime environments. |
| No target for a call | Was that branch executed? Are both sources included? Is the call form supported? Is the relevant worker reported? |
| Navigation only shows a static target | Enable CallNav, load the intended run, and check source hashes. A static result alone does not demonstrate runtime recording. |
| Several destinations appear | The site observed several implementations, or another Python provider added static definitions. Use the native chooser. |
| Runtime target unavailable after editing | Save or restore code matching the recording, or record again. |
| New recording is not shown | Run **CallNav: Load Latest Runtime Index** again. |
| Latest index cannot be found | Open the recorded root folder, select `index.json` explicitly, or set `callnav.indexPath`. |
| Remote recording cannot open local files | Use Remote-SSH on the original host and checkout, or record in the new location. There is no path remapping. |
| Output directory is rejected | Choose a new or empty directory; separate runs never overwrite existing nonempty output. |
