# Track_any_call

**Run Python once, then Ctrl/Cmd+click a call to find the implementation that actually ran.**

Track_any_call ships the **CallNav** recorder and VS Code extension. Package, CLI, and editor commands currently use the CallNav name.

[中文](README.zh-CN.md) · [Usage guide](docs/usage.md) · [How it works](docs/architecture.md)

## Features

- **Real dynamic targets:** follow registries, selected subclasses, decorators, `functools.partial`, and supported async calls.
- **Native VS Code navigation:** use Ctrl/Cmd+click or Go to Definition, with multiple observed targets in the standard chooser. Python/Pylance stays active.
- **Threads, subprocesses, and Ray:** collect participating Python processes; opt in to connect Ray Actor methods and constructors to worker execution.
- **Source checks:** fingerprints prevent stale recordings from opening mismatched definitions.

## Install

Requires **Python 3.11+** and **VS Code 1.85+**. Activate your application's normal Python environment first.

```bash
git clone https://github.com/Zethan06/Track_any_call.git
cd Track_any_call
python -m pip install .
python vscode/scripts/package.py
```

In VS Code, run **Extensions: Install from VSIX…** and select the generated `.vsix`. For Remote-SSH, install it on the remote workspace host.

## Record and navigate

Run your existing launch script from its usual working directory:

```bash
cd /path/to/your-project
callnav record --root "$PWD" -- bash ./launch.sh
```

Then open that project in VS Code:

1. **CallNav: Load Latest Runtime Index**
2. **CallNav: Enable Runtime Navigation**
3. Ctrl/Cmd+click an observed call.

For Ray and external source trees:

```bash
callnav record --ray --root "$PWD" --include-root ../external-library -- bash ./launch.sh
```

For a GPU-free demo, run `callnav record --root "$PWD" -- bash examples/mini_pipeline/launch.sh` from this checkout. See [examples](examples/README.md) for the call sites to try.

## Scope

Only executed, supported calls have runtime destinations. Profiling adds overhead; native/CUDA internals and HTTP causal links are outside the graph. Ray workers must receive the recording environment and share accessible paths.

Recordings live in `.callnav/` and use your checkout's paths. Keep matching source, record again after moving or editing it, and add `.callnav/` to `.gitignore`. Review source paths and launch arguments before sharing records.

[Detailed setup and troubleshooting](docs/usage.md) · [Contributing and tests](CONTRIBUTING.md) · [MIT license](LICENSE)
