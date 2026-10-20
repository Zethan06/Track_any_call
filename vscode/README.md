# CallNav for VS Code

Ctrl/Cmd+click a Python call site to open implementations observed during a recorded run. CallNav adds definitions to your normal source editor; it needs no browser or running backend service.

The accompanying Python recorder is required to create an index. The recorder and every Python process being recorded require Python 3.11+. In the CallNav source checkout, see `README.md` for installation and recording instructions, and `docs/usage.md` for the complete usage guide.

## Build and install

Requires VS Code 1.85+ and Python 3.11+. From the CallNav repository root:

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

## Navigate a recording

After recording your command:

1. Open the source folder used as the recorder's `--root`.
2. Run **CallNav: Load Latest Runtime Index** from the Command Palette.
3. Run **CallNav: Enable Runtime Navigation**.
4. Ctrl/Cmd+click a recorded call expression or use **Go to Definition**.
5. Use **CallNav: Disable Runtime Navigation** to stop adding runtime targets.

Several destinations appear in VS Code's native definition chooser or Peek. For `runner.run.remote(config)`, a supported recorded Ray Actor call accepts clicks on either `run` or `remote`.

The status bar shows the loaded run and whether runtime navigation is enabled. Click it to toggle navigation. A newly recorded index is not automatically reloaded; run **Load Latest Runtime Index** again.

## Commands and settings

| Command | Purpose |
| --- | --- |
| CallNav: Load Latest Runtime Index | Load the configured index for the active workspace folder. |
| CallNav: Load Runtime Index… | Choose a particular run's `index.json`. |
| CallNav: Enable Runtime Navigation | Add recorded definitions; loads the latest index if none is loaded. |
| CallNav: Disable Runtime Navigation | Stop adding recorded definitions. |
| CallNav: Toggle Runtime Navigation | Toggle the current state. |

The default setting is:

```json
{
  "callnav.indexPath": "${workspaceFolder}/.callnav/latest.json"
}
```

`${workspaceFolder}` resolves against the active document's workspace folder, falling back to the first open folder. A relative setting resolves from that folder. An index setting controls which recording is loaded; it does not remap recorded source paths.

## Coexistence and source checks

Documents remain in **Python** mode. Pylance completion, diagnostics, and static definitions stay active. VS Code combines definitions from its providers, so static targets may appear alongside runtime targets. A static result is not proof that CallNav observed that target.

CallNav checks source fingerprints, including wrapper chains. Changed, missing, or unsaved source blocks the affected runtime destination. Restore matching source or record again. Ambiguous call sites without a supported precise target are skipped.

The index stores absolute paths resolved at recording time. Use the same checkout and host; copying an index to a differently located checkout does not relocate it. No server-specific checkout is built into the extension.

## Development

From the repository root:

```bash
node --test vscode/test/unit.test.js
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
