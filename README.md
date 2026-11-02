# Track_any_call

Track_any_call records Python calls at runtime to show which implementation a dynamically bound call actually used. For functions loaded from registries or subclass methods called through abstract interfaces, you can jump from the call site in VS Code to the implementation observed during the run.

The recorder, packages, and editor commands currently use the name CallNav.

[中文](README.zh-CN.md) · [Usage guide](docs/usage.md) · [How it works](docs/architecture.md)

## Install

Requires Python 3.11+ on the workspace host and VS Code 1.85+. Download the `callnav-release` artifact from a successful [Checks build](https://github.com/Zethan06/Track_any_call/actions/workflows/checks.yml), unzip it, then install its `.vsix` with **Extensions: Install from VSIX…**. The extension includes the Python recorder; plugin capture needs no separate pip installation or source checkout. With Remote-SSH, install it on the remote host.

Version tags publish a stable `callnav.vsix` asset through the release workflow. After the first tagged release, the [direct VSIX download](https://github.com/Zethan06/Track_any_call/releases/latest/download/callnav.vsix) provides a single download link.

To build the same VSIX from source:

```bash
git clone https://github.com/Zethan06/Track_any_call.git
cd Track_any_call
python vscode/scripts/package.py
```

The last command prints the extension file's location. You can also install it with `code --install-extension /path/to/callnav.vsix`.

## Use

After installing the VSIX, open a **new VS Code terminal**. The extension makes its bundled `callnav` command available there without pip. If needed, run **CallNav: Enable Terminal Command** from the Command Palette, then open a new terminal. Activate your usual environment, change to your project directory, and add `callnav` before your normal command:

```bash
cd /path/to/your-project
callnav bash ./launch.sh
callnav python train.py --steps 1
```

No per-project JSON configuration is needed. This prefix uses the current directory as its source root and enables supported Ray capture automatically. Your existing Conda environment, GPU selection, model/data parameters, and script arguments still come from your command or script. Arguments after the executable, including `--help` and `--version`, belong to that command.

The terminal command uses Python from that terminal's current environment, which must be Python 3.11+. CallNav adds its command directory to new VS Code terminals without editing shell startup files. For Remote-SSH, install the extension on the remote host and open a new remote terminal.

Put optional recorder flags before the command. Use `--include-root` for external source and `--no-ray` to disable Ray capture:

```bash
CUDA_VISIBLE_DEVICES=0,1 callnav --include-root ../external-library -- bash ./launch.sh
callnav --no-ray -- python train.py --steps 1
```

Every recording has its own history directory; previous runs are kept. In VS Code, open the same project and run **CallNav: Select Recording…**. Choose a completed run by date, command, status, and edge count; selecting it loads that index and enables runtime navigation. Ctrl/Cmd+click a recorded call to open its observed implementation. **CallNav: Load Latest Runtime Index** remains available as a shortcut to the latest index.

You can also capture entirely from the extension, using its bundled recorder without installing the CLI: right-click a `.sh` or `.py` file and choose **CallNav: Record Script…**, or use **CallNav: Record Command…**. Successful captures load automatically. **CallNav: Stop Recording** stops an editor capture. Optional settings are described in the [extension guide](vscode/README.md#commands-and-settings).

The explicit form `callnav record --ray --root "$PWD" -- bash ./launch.sh` remains supported; unlike the short prefix, `record` requires `--ray` to enable Ray capture. Ray workers need to inherit the recording environment and have access to the source and recording directories. See the [Ray setup instructions](docs/usage.md#ray-and-framework-launchers).

For a shell outside VS Code, such as a normal SSH session, install the standalone CLI once in your usual Python environment:

```bash
python -m pip install /path/to/callnav-0.6.10-py3-none-any.whl
# Or, from a source checkout:
python -m pip install -e /path/to/Track_any_call
```

Recordings are saved under your project's `.callnav/` directory. CallNav does not delete old run directories; `latest.json` is a convenience copy, not the only saved run. Add `.callnav/` to `.gitignore` and record again after editing source or moving the checkout. Check paths and launch arguments before sharing a recording.

Only calls that ran and were successfully recorded have runtime destinations. Version 0.6.10 automatically selects lower-overhead capture on Python 3.12/3.13 and retains the compatible backend on Python 3.11; no command or configuration changes are needed. Recording still adds overhead, so start with a short run. Calls inside C/CUDA code are not recorded.

[Development and tests](CONTRIBUTING.md) · [MIT license](LICENSE)
