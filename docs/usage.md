# Usage guide

Prefix your existing command with `callnav`, then select its recording in VS Code. Each run keeps its own history; no per-project JSON file is required. The extension can also capture scripts with its bundled recorder. Both produce the same runtime index.

## Install the extension

Install a built VSIX with **Extensions: Install from VSIX…**. The `callnav-release` artifact in a successful repository Checks run includes it. The VSIX bundles the recorder, so plugin capture needs no pip installation. No published PyPI or Marketplace package is assumed.

Python 3.11+ is required for the recorder and every Python process you want to record. Its instruction-level source positions distinguish calls on the same line. A child interpreter older than 3.11 is rejected during recording startup.

To capture from the editor, open the project's folder and run **CallNav: Record Script…**, or use the script's right-click action. **CallNav: Record Command…** accepts your normal shell launch command. A successful task automatically loads that run's exact index and enables navigation. See the [extension guide](../vscode/README.md) for settings, interpreter selection, cancellation, and task behavior.

## Use the terminal command

After installing the VSIX, open a **new VS Code terminal**. The extension adds its bundled `callnav` command to new terminals without a pip installation or changes to your shell startup files. If needed, run **CallNav: Enable Terminal Command** from the Command Palette and open another terminal.

Activate your application's normal Conda/virtualenv environment in that terminal. The command uses Python from the terminal's current environment, which must be Python 3.11+. You can then use `callnav bash ./launch.sh` from the project directory. With Remote-SSH, install the extension and open the new terminal on the remote workspace host.

For an external shell, such as a normal SSH session outside VS Code, install the standalone CLI once from a wheel or source checkout:

```bash
python -m pip install /path/to/callnav-0.6.10-py3-none-any.whl
# Or install a source checkout:
python -m pip install -e /path/to/Track_any_call
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
callnav bash ./launch.sh --config ./configs/debug.yaml
```

Or launch Python directly:

```bash
callnav python train.py --steps 1
callnav python -m your_package --config ./config.yaml
```

The short prefix uses the current directory as its source root and enables supported Ray capture automatically. The `--steps 1` option above belongs to the application. Once the command starts, all following arguments belong to that command, including `--help`, `--version`, `--root`, and `--no-ray`; CallNav does not consume them.

Put CallNav options before the command, followed by `--` for an explicit boundary:

```bash
callnav --root . --include-root ../external-library -- bash ./launch.sh
callnav --no-ray -- python train.py --steps 1
callnav bash ./launch.sh --help
```

CallNav runs this argument vector without adding a shell layer. Keep shell expansion, redirection, or pipelines inside your existing `.sh`, or explicitly use `bash -c '...'`. Recording does not change the working directory. It preserves normal terminal output and returns the command's exit status.

Use the same Conda/virtualenv environment, Python executable, GPU selection, model/data settings, and launch arguments your application normally needs. Environment assignments remain ordinary shell syntax:

```bash
CUDA_VISIBLE_DEVICES=0,1 callnav bash ./launch.sh --config ./configs/debug.yaml
```

If a script selects another interpreter or activates another environment, that interpreter must support CallNav and be able to read the injected package/bootstrap paths. Start with a small representative workload: initialization, rollout, training, evaluation, and cleanup only happen if your command executes them.

CallNav does not modify a generic script to stop after one step. Use the framework's own limits; the optional synchronous Slime adapter below has a specific supported iteration boundary.

## Recorder options

```text
callnav [--root PATH] [--include-root PATH ...] [--output PATH]
        [--ray | --no-ray] [--adapter slime] [--iterations N]
        [--] COMMAND [ARG ...]
callnav record [--root PATH] [--include-root PATH ...] [--output PATH]
               [--ray | --no-ray] [--adapter slime] [--iterations N]
               -- COMMAND [ARG ...]
callnav merge RUN_DIRECTORY
```

The short prefix defaults to Ray enabled. The existing `callnav record` form keeps its prior default of Ray disabled; add `--ray` when using that form for a Ray application. `callnav --help` and `callnav --version` describe CallNav itself. These flags after a command are passed to the child instead.

| Option | Meaning |
| --- | --- |
| `--root PATH` | Primary source tree; defaults to the current directory. Owns `.callnav/latest.json`. |
| `--include-root PATH` | Additional source tree, repeatable. Both endpoints of an ordinary call must be within configured trees. |
| `--output PATH` | New or empty recording directory; existing nonempty output is rejected. |
| `--ray` / `--no-ray` | Enable/disable supported Ray Actor matching and the PATH-based Ray Job CLI environment shim. Enabled by default for the short prefix, disabled by default for `record`. |
| `--adapter slime` | Enable the experimental synchronous Slime integration, including its Ray CLI environment shim. |
| `--iterations N` | Complete outer Slime iterations; requires the adapter and a positive count. Default with the adapter is 1. |

Paths are resolved from the recorder's current working directory. Source roots select collection scope; they do not install packages or change Python's import resolution.

Include a sibling library checkout as needed:

```bash
callnav --root "$PWD" \
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
    recordings/             # catalog entries pointing to saved run directories
      <catalog-id>.json
    <run-id>/
      run.json              # launch metadata and exit status
      index.json            # merged navigation index
      shards/               # per-process reports
      .bootstrap/           # startup hook for this run
```

Optional backends add their own event/adapter files. The terminal prints the exact output directory and any coverage warnings. Each invocation gets a new run ID. Existing nonempty output directories are rejected, and recording does not delete earlier run directories. `latest.json` is a convenience copy of an index; replacing it does not replace the saved run.

The catalog contains references to each run, including runs saved outside `.callnav/` with `--output`. It reads launch information and status from that run's actual files. The latest convenience index and catalog belong to the primary source root even when `--output` points elsewhere. Existing direct `.callnav/<run-id>/` recordings and a legacy `latest.json` are also discoverable; old data does not need to be moved.

In VS Code:

1. Open the primary source folder.
2. Run **CallNav: Select Recording…**.
3. Choose a completed recording by date, original command, status, and edge count. It loads that run and enables runtime navigation.
4. Ctrl/Cmd+click an observed call, or use **Go to Definition**.

The picker also shows running, failed, empty, unreadable, or status-unknown recordings; those are not automatically loaded as successful recordings. A legacy index without its run manifest has unknown status. Failed or stopped runs keep any data they flushed. For an explicit partial-data or legacy-index inspection, use **CallNav: Load Runtime Index…** to select its `index.json`, then enable navigation if desired. Canceling the picker leaves the current recording unchanged.

**CallNav: Load Latest Runtime Index** and **CallNav: Enable Runtime Navigation** remain available. After another run or merge, select or reload the index to use its updated contents. In a multi-root workspace, the history command lets you choose a project folder. Loading the latest index uses the active document's workspace folder; you can select a file explicitly if needed.

CallNav adds definitions to ordinary Python documents. Static providers remain active, including for calls the recording did not observe. Multiple observed implementations and static definitions may appear in VS Code's native chooser.

The extension verifies recorded source fingerprints. Save or restore matching source if a file is dirty or changed; record again after changing code. The current format uses absolute paths from your own runtime. It has no path remapping: when changing checkout location, container, or host, record again there.

Add `.callnav/` to your application's `.gitignore`. Recordings contain source paths, command-line arguments, and process arguments. They do not serialize function argument or return values, but launch arguments may still contain sensitive data.

## Remote-SSH

1. Open the application's remote source folder with VS Code Remote-SSH.
2. Install the VSIX in that remote workspace extension host.
3. Open a new remote terminal, activate your usual environment, and run `callnav bash ./launch.sh`. Alternatively, select the remote interpreter and record the script from the editor.

Use **CallNav: Select Recording…** to load a saved run. For a normal SSH terminal outside VS Code, install the standalone recorder once on that host.

The extension is a workspace extension. It reads source files and the index on that host; no browser UI or separate CallNav service is needed. Recording on a remote host and opening a local copy at another path does not work through an index setting alone.

## Ray and framework launchers

### Ray Actor calls

```bash
callnav python ray_main.py
```

The short prefix enables Ray capture. The equivalent explicit form is `callnav record --ray --root "$PWD" -- python ray_main.py`. Use `callnav --no-ray -- ...` when Ray capture is unwanted.

The experimental backend uses Ray internals and was tested with **Ray 2.43.0 and 2.58.0**. Its executor hook preserves version-specific arguments such as `actor_imported`. Other versions need their own validation. Your existing environment must provide Ray; CallNav does not depend on or install it.

The backend connects supported Actor method submissions and constructors to worker entries using task/actor IDs. It supports synchronous and async Actor methods, inherited Python constructors, and observed decorator forwarding chains. Fetching an existing named Actor does not create a constructor edge. Calls without matching execution evidence do not become definite runtime destinations.

Both submitters and workers must start with collection enabled. Bootstrap, package, source, and recording directories must be accessible at the same paths. The adapter merges recording environment variables into observed `ray.init(runtime_env=...)` calls while preserving existing settings. This is not a multi-host deployment service.

Activate the final environment that provides your intended `ray` executable **before** running `callnav`. The recorder captures that executable and places its temporary shim first on PATH. A later `conda activate` or `export PATH=...` inside the launch script can bypass the shim, or leave it using the Ray executable captured before the environment changed. Move that environment setup before recording.

For PATH-based `ray job submit [options] -- command`, enabled Ray capture supplies a temporary CLI shim that merges recording variables and the injected `PYTHONPATH` into `--runtime-env-json`. Existing environment settings and command arguments are preserved. A launcher can therefore keep its ordinary blocking job submission:

```bash
callnav bash ./ray-launch.sh
```

The shim rejects `--no-wait` and `--runtime-env` files: it must wait for the job, and currently supports JSON runtime environments only. An absolute `ray` executable bypasses the PATH shim. When a script starts Ray daemons with `ray start`, the shim removes recording opt-in variables from those long-lived daemons; the submitted job receives its own explicit recording environment. Containers, external services, and launchers replacing environments still require explicit integration. Inspect reported processes and warnings rather than assuming all workers were captured.

Regular Ray remote functions, generator Actor methods, pre-existing uninstrumented workers, and HTTP causal links are outside this backend's supported matching scope.

### Frameworks such as VERL and asynchronous Slime

Prefix your normal launch command with `callnav`, keep its existing framework-specific step limits, and include external source trees you want to navigate. The prefix enables supported Ray capture automatically. No generic framework integration is required for ordinary instrumented Python calls.

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
| Suspended calls | Immediate awaits and proven native coroutine, async-generator and generator creations can map their first actual entry. Creation proof can come from a real Python argument, a literal return, an immediate generator assignment to a local, or iteration over a compiler-verified literal tuple of creations. Simple synchronous list comprehensions with one clause and no filters can retain per-element creation evidence through their actual returned list or a Python consumer's actual arguments. Direct `async for` over a native async-generator call requires a verified uninterrupted native iteration instruction sequence and actual initial frame entry. This includes `asyncio.run`, Python argument handoff across threads, and synchronous `__wrapped__` decorators returning the exact newly created object. Pre-load callable identity and object or native protocol lineage must agree. Precreated objects without creation evidence, arbitrary factories, unproven assigned objects and resumes remain excluded. Existing debugger trace hooks disable creation tracing. |
| Registered callbacks | Exact stdlib `threading.Thread(target=...)` targets and native `asyncio.Task.add_done_callback(...)` registrations map only after the registered callback actually executes. Task callbacks also require a receipt from native Task completion scheduling the exact Handle. When the application imports uvloop, an optional bridge accepts native Task completion only after its coroutine has closed and confirms the receipt wrapper actually entered the registered callback; manual scheduling is excluded. Removed, never-run and manually scheduled lookalikes produce no registration edge; custom Thread/Task subclasses and callbacks registered after Task completion remain outside this proof. |
| Returned callables | Immediate `registry.get(key)(*args, **kwargs)` can map the observed constructor or function when the factory returns its actual callable and the target enters without an intervening profile event. Argument evaluation that calls Python or native code invalidates this proof. |
| Hydra | Root `hydra.utils.instantiate(...)` calls and `@hydra.main` entrypoints can map the resolved target's actual Python entry, with Hydra forwarding source fingerprints. Recursive argument objects, deferred partials, help-only invocations, and targets that never enter are not projected onto the root call. Hydra remains an optional application dependency. |
| Native code | C/CUDA/JIT internals and native/HTTP causal relationships are not reconstructed. Native callbacks without a safe call-site proof remain ambiguous. |
| Unresolvable expressions | Ambiguous or unproven call relationships are skipped for runtime navigation. |
| Other profilers and debuggers | Existing profile hooks prevent recording in that process. Existing trace hooks disable scheduled coroutine capture and produce a coverage warning; CallNav preserves the debugger's hook. Other tools can replace hooks later; inspect process reports for conflicts. |
| Isolated startup | `python -S`, `python -I`, cleared environments, and inaccessible bootstrap paths can bypass collection. |

Recording adds CPU overhead. Version 0.6.10 automatically uses local Python entry/return events on source and observed callable code on Python 3.12 and 3.13. New source code and functions reconstructed from code objects are armed before execution; actual callees and supported callbacks are armed before dispatch. Sensitive construction and binding proofs temporarily retain global Python events, and exception and Task callback invalidation remain covered. Irrelevant audit events return before registry processing. Runtime entry receipts and matching source fingerprints remain required for navigation. If local monitoring cannot be installed, the native profile dispatcher with selective instruction monitoring remains a fallback. No extra command option or environment configuration is required.

Code caches retain code identities instead of recursively hashing large code objects, and source structure and call-site plans are indexed once. Only structurally relevant instruction offsets enter proof callbacks, including branch/exception destinations and immediate binding/iteration validation. Unknown code is kept eligible for later wrapper promotion. Ray/Hydra entry observers register only when compatible framework boundaries load, and ordinary module bindings are reread with conservative descriptor checks.

Python 3.11 and processes where the efficient backend cannot be installed retain the compatible trace/profile backend automatically. Loop-containing Python 3.11 frames use line windows, reopening opcode capture at the next line or backward jump; pending receipts still observe their immediate next instruction. Short functions and incomplete line mappings retain full opcode tracing. On Python 3.12+, an unavailable monitoring slot uses full opcode tracing. Existing debugger/profile hooks retain the documented conflict behavior. No extra command option or environment configuration is required. Large import trees and Python-heavy loops can still be expensive; measure a short run on your workload before recording a long job.

For a CPU-only recorder benchmark from the source checkout:

```bash
python scripts/benchmark_recorder.py --repeats 3
python scripts/benchmark_recorder.py --workload module --repeats 3
python scripts/benchmark_recorder.py --workload imports --repeats 3
python scripts/benchmark_recorder.py --workload opcodes --repeats 3
# Compare an older preserved source tree under the same interpreter:
python scripts/benchmark_recorder.py --baseline-source /path/to/older-checkout/src
```

The benchmark reports complete subprocess times, checks every expected target and invocation count, and requires identical call graphs when comparing versions. It uses no model, GPU, network, or third-party dependencies. These synthetic timings are not a guarantee for training or model inference.

Processes normally report at shutdown and periodically flush. A process killed with `SIGKILL`, `os._exit`, or a worker still running when the launcher exits may leave partial data. The index lists **reported** processes and diagnostics; an absent process is not evidence that it was recorded.

After late workers have flushed, refresh the index:

```bash
callnav merge .callnav/<run-id>
```

Use the actual directory printed by the recorder instead of the placeholder. Merge reads existing shards; it cannot recover events that were never flushed. It refreshes `latest.json` only when that file still refers to the same run, so merging an older run does not replace a newer latest index.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `callnav` is not found | In VS Code, run **CallNav: Enable Terminal Command**, then open a new terminal. Outside VS Code, install the CLI once from a wheel/source checkout and activate that environment; `python -m callnav bash ./launch.sh` also works when installed there. The VSIX does not create a system-wide shell command. |
| No Python process reported | Check interpreter versions, `PYTHONPATH`, `-S/-I`, environment clearing, shared paths, and job runtime environments. |
| No target for a call | Was that branch executed? Are both sources included? Is the call form supported? Is the relevant worker reported? |
| Navigation only shows a static target | Enable CallNav, load the intended run, and check source hashes. A static result alone does not demonstrate runtime recording. |
| Several destinations appear | The site observed several implementations, or another Python provider added static definitions. Use the native chooser. |
| Runtime target unavailable after editing | Save or restore code matching the recording, or record again. |
| New recording is not shown | Run **CallNav: Select Recording…** again in the recorded project folder, or load the latest index. |
| Latest index cannot be found | Open the recorded root folder, use **CallNav: Select Recording…**, or select `index.json` explicitly. |
| Remote recording cannot open local files | Use Remote-SSH on the original host and checkout, or record in the new location. There is no path remapping. |
| Output directory is rejected | Choose a new or empty directory; separate runs never overwrite existing nonempty output. |
