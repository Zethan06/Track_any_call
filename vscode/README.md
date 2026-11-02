# CallNav for VS Code

Ctrl/Cmd+click a Python call site to open implementations observed during a recorded run. CallNav adds definitions to your normal source editor; it needs no browser or running backend service.

The VSIX includes its Python recorder. Python 3.11+ must be available on the workspace host and in every process being recorded. Plugin capture requires no pip installation, npm dependencies, or CallNav source checkout. Your project still needs its usual dependencies, models, data, and launch environment.

## Build and install

Requires VS Code 1.85+ and Python 3.11+. Download and unzip the `callnav-release` artifact from a successful repository Checks build, then use **Extensions: Install from VSIX…** on the included `.vsix`. The extension has not been published to Marketplace.

To build the same installation file from the CallNav repository root:

```bash
python vscode/scripts/package.py
```

The command prints the generated VSIX path. Run **Extensions: Install from VSIX…** in VS Code and select that file. Alternatively:

```bash
callnav_vsix="$(python vscode/scripts/package.py)"
code --install-extension "$callnav_vsix"
```

No npm install or compilation is needed. Node.js is needed only for JavaScript development tests.

For **Remote-SSH**, install the VSIX in the remote workspace extension host. Record on the remote host and open the same source checkout there.

## Capture from the editor

Open the project folder in a trusted workspace. Right-click a `.sh`, `.bash`, or `.py` file and choose **CallNav: Record Script…**. The editor's record button and initial CallNav status bar entry do the same. The project folder is both the source root and the default working directory. No per-project JSON configuration is required.

CallNav starts the existing recorder as a visible VS Code task. Shell scripts run with `callnav.shellPath` (default `bash`); Python scripts run with the selected interpreter. The command and script path are passed as separate arguments, including paths with spaces. Set `callnav.scriptArguments` for literal arguments, or use **CallNav: Record Command…** for a full shell command with pipes, redirection, or shell expansion.

Select the interpreter using the Python extension, or set `callnav.pythonPath` explicitly. Relative interpreter paths resolve against the workspace folder even with a different working directory; bare executable names use PATH. Without either, CallNav uses `python3` (`python` on Windows). The selected interpreter's directory is prepended to PATH. The task inherits the workspace extension host's environment plus `callnav.environment`; activation in a separate integrated terminal does not change that environment. Keep Conda/virtualenv setup in your launch script or select the appropriate interpreter and environment settings.

A successful task automatically loads **that capture's** `index.json` and enables runtime navigation. Coverage warnings remain visible in the CallNav output channel. **CallNav: Stop Recording**, the status bar, or VS Code's task controls stop a capture. Failed or stopped tasks preserve the previously loaded index; any flushed partial recording is merged for explicit inspection. Stopping cannot undo training side effects or stop independently managed services/clusters created by a script. Only one capture runs at a time in a window.

For Ray launchers, enable `callnav.ray`. Add external checkouts through `callnav.includeRoots`. For VERL or asynchronous Slime, use the launch script's normal one-step limits. The optional `slime` adapter only budgets synchronous `train.py`; leave it empty for `train_async.py`.

## Capture from your normal terminal

After installing the VSIX, open a **new VS Code terminal**. The extension makes its bundled `callnav` command available there without pip. If the command is unavailable, run **CallNav: Enable Terminal Command** from the Command Palette and open a new terminal. Activate your usual environment, enter your project directory, and prefix the original command:

```bash
callnav bash ./launch.sh
CUDA_VISIBLE_DEVICES=0,1 callnav --include-root ../external-library -- bash ./launch.sh
callnav --no-ray -- python train.py --steps 1
```

The short prefix uses the current directory as its source root and enables supported Ray capture by default. It keeps your command's arguments, including child `--help` and `--version` flags. Your existing environment, GPU selection, model/data parameters, and `.sh` configuration continue to apply. Put optional CallNav flags before the command; no workspace settings file is needed.

The command uses Python from the terminal's current environment, which must be Python 3.11+. CallNav adds its command directory to new VS Code terminals without editing shell startup files. Existing terminals may need to be reopened. For Remote-SSH, install the extension and open the terminal on the remote host.

The existing `callnav record --ray --root "$PWD" -- bash ./launch.sh` form remains available with its previous Ray-disabled default.

For a shell outside VS Code, install the standalone CLI once in your usual Python environment:

```bash
python -m pip install /path/to/callnav-0.6.10-py3-none-any.whl
# Or:
python -m pip install -e /path/to/Track_any_call
```

The VSIX does not install a system-wide command. Its editor capture and new-terminal command continue to work without pip.

## Navigate a recording

After recording your command:

1. Open the source folder used as the recorder's `--root`.
2. Run **CallNav: Select Recording…** from the Command Palette.
3. Choose a completed run by date, original command, status, and edge count. Selection loads its index and enables runtime navigation.
4. Ctrl/Cmd+click a recorded call expression or use **Go to Definition**.
5. Use **CallNav: Disable Runtime Navigation** to stop adding runtime targets.

Several destinations appear in VS Code's native definition chooser or Peek. For `runner.run.remote(config)`, a supported recorded Ray Actor call accepts clicks on either `run` or `remote`.

The status bar shows the loaded run and whether runtime navigation is enabled. Click it to toggle navigation. Successful captures started by the plugin load automatically. After CLI captures, use **Select Recording…** to choose any saved run. **Load Latest Runtime Index** remains a convenient shortcut; **Enable Runtime Navigation** can enable an index loaded manually.

Each run has its own directory under `.callnav/`, or in the explicit `--output` location. The project catalog under `.callnav/recordings/` also lists external output directories. The picker discovers existing `.callnav/<run-id>/` directories and legacy `latest.json` files. CallNav does not delete old run directories, and replacing `latest.json` does not erase history.

Running, failed, empty, unreadable, or status-unknown runs are not automatically loaded as successful recordings. A legacy index without its run manifest has unknown status. For explicit inspection of a flushed partial or legacy index, use **CallNav: Load Runtime Index…**; **Load Latest Runtime Index** also remains available for a legacy latest index. Canceling selection keeps the current index and navigation state. In a multi-root workspace, choose the project folder first.

## Commands and settings

| Command | Purpose |
| --- | --- |
| CallNav: Record Script… | Run a selected shell/Python script and load its successful capture. |
| CallNav: Record Command… | Enter an existing shell command to capture. |
| CallNav: Stop Recording | Stop the active capture task. |
| CallNav: Enable Terminal Command | Make bundled `callnav` available to newly opened VS Code terminals without pip. |
| CallNav: Select Recording… | Browse saved runs by date, command, status, and edge count; select a completed run to load it and enable navigation. |
| CallNav: Load Latest Runtime Index | Load the configured index for the active workspace folder. |
| CallNav: Load Runtime Index… | Choose a particular run's `index.json`. |
| CallNav: Enable Runtime Navigation | Add recorded definitions; loads the latest index if none is loaded. |
| CallNav: Disable Runtime Navigation | Stop adding recorded definitions. |
| CallNav: Toggle Runtime Navigation | Toggle the current state. |

Optional capture settings are available in VS Code's Settings UI; no JSON editing is needed. They are scoped to the selected workspace folder:

| Setting | Purpose |
| --- | --- |
| `callnav.pythonPath` | Python executable; empty selects the Python extension environment or platform default. |
| `callnav.shellPath` | Shell for scripts and command input; default `bash`, requires `-c` for command input. |
| `callnav.scriptArguments` | Array of literal script arguments. |
| `callnav.workingDirectory` | Working directory; defaults to `${workspaceFolder}`. |
| `callnav.includeRoots` | Array of extra source directories, relative to the workspace folder if needed. |
| `callnav.ray` | Enable supported Ray Actor joins for editor captures; defaults to false. The standalone short CLI prefix defaults to true. |
| `callnav.adapter` | Optional synchronous Slime iteration adapter; defaults to empty. |
| `callnav.iterations` | Positive budget when the Slime adapter is selected; defaults to 1. |
| `callnav.environment` | String-valued environment variables merged into the host environment. |

For a two-GPU script that already sets `CUDA_VISIBLE_DEVICES` and activates its environment, keep those settings in the original script. To capture from a terminal, use the prefix examples above. For editor capture, select the interpreter and set **CallNav: Ray** or **CallNav: Include Roots** in the Settings UI if needed.

The optional `callnav.indexPath` setting defaults to `${workspaceFolder}/.callnav/latest.json`. **Select Recording…** lets you choose history without changing this setting.

`${workspaceFolder}` resolves against the active document's workspace folder, falling back to the first open folder. A relative setting resolves from that folder. An index setting controls which recording is loaded; it does not remap recorded source paths.

## Coexistence and source checks

Documents remain in **Python** mode. Pylance completion, diagnostics, and static definitions stay active. VS Code combines definitions from its providers, so static targets may appear alongside runtime targets. A static result is not proof that CallNav observed that target.

CallNav checks source fingerprints, including wrapper chains. Changed, missing, or unsaved source blocks the affected runtime destination. Restore matching source or record again. Ambiguous call sites without a supported precise target are skipped.

The index stores absolute paths resolved at recording time. Use the same checkout and host; copying an index to a differently located checkout does not relocate it. No server-specific checkout is built into the extension.

## Development

From the repository root:

```bash
node --test vscode/test/*.test.js
python vscode/scripts/package.py
```

To run the genuine VS Code API suite from `vscode/`:

```bash
code --extensionDevelopmentPath="$PWD" \
  --extensionTestsPath="$PWD/test/extension-host.js" \
  --user-data-dir=/tmp/callnav-vscode-user \
  --extensions-dir=/tmp/callnav-vscode-extensions
```

Use a fresh test profile. For a Pylance compatibility check, install the Python and Pylance extensions into that profile first. The suite checks provider results, native Go to Definition, static-provider coexistence, unique/multiple targets, stale and unsaved source, and unchanged Python language mode.

An optional physical input phase is available through `CALLNAV_TEST_MOUSE_DIR`: set it to a fresh directory, wait for `ready.json`, Ctrl+click the first `fn` call in the active test editor, then create `clicked.json`. The suite writes `result.json` with the actual destination. API tests alone do not prove mouse interaction. Remote-SSH and client-specific extension combinations require their own integration checks.

The packaged capture suite is `test/capture-host.js`. Extract a freshly built VSIX, use its `extension/` as `--extensionDevelopmentPath`, open a disposable empty workspace, and set `CALLNAV_CAPTURE_WORKSPACE` to that folder and `CALLNAV_CAPTURE_PYTHON` to a clean Python environment. Run with `--extensionTestsPath` pointing to this suite. It checks bundled capture without pip, quoted paths/arguments, environment inheritance, auto-load and native navigation, task failure/cancellation, duplicate rejection, invalid interpreters, and a workspace module attempting to shadow the bundled backend.
