# Track_any_call

Track_any_call records Python calls at runtime to show which implementation a dynamically bound call actually used. For functions loaded from registries or subclass methods called through abstract interfaces, you can jump from the call site in VS Code to the implementation observed during the run.

The recorder, packages, and editor commands currently use the name CallNav.

[中文](README.zh-CN.md) · [Usage guide](docs/usage.md) · [How it works](docs/architecture.md)

## Install

Requires Python 3.11+ and VS Code 1.85+. Activate the Python environment you normally use for your project, then install:

```bash
git clone https://github.com/Zethan06/Track_any_call.git
cd Track_any_call
python -m pip install .
python vscode/scripts/package.py
```

The last command prints the extension file's location. In the VS Code Command Palette, run `Extensions: Install from VSIX…` and select the generated `.vsix` file. With Remote-SSH, install the extension on the remote host.

## Use

From your project's usual working directory, put `callnav record` before your existing launch command:

```bash
cd /path/to/your-project
callnav record --root "$PWD" -- bash ./launch.sh
```

Put your original command and its arguments after `--`. When the recording finishes, open the same project folder in VS Code, then:

1. Run `CallNav: Load Latest Runtime Index`.
2. Run `CallNav: Enable Runtime Navigation`.
3. Ctrl+click a call. On macOS, use Cmd+click.

Add `--ray` to record Ray Actor calls. Use `--include-root` for source outside your project directory:

```bash
callnav record --ray --root "$PWD" --include-root ../external-library -- bash ./launch.sh
```

Ray workers need to inherit the recording environment and have access to the source and recording directories. See the [Ray setup instructions](docs/usage.md#ray-and-framework-launchers).

Recordings are saved in your project's `.callnav/` directory. Add it to `.gitignore` and record again after editing source or moving the checkout. Check paths and launch arguments before sharing a recording.

Only calls that ran and were successfully recorded have runtime destinations. Recording adds overhead, so start with a short run. Calls inside C/CUDA code are not recorded.

[Development and tests](CONTRIBUTING.md) · [MIT license](LICENSE)
