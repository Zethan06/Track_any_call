# CallNav

**Record a Python program once, then Ctrl/Cmd+click its observed implementations in VS Code.**

CallNav connects a call site to the Python function that actually ran. It helps with registries, dynamically selected subclasses, decorators, `functools.partial`, and supported Ray Actor calls. Navigation happens in your normal `.py` editor, using VS Code's definition UI.

[中文](README.zh-CN.md) · [Usage guide](docs/usage.md) · [How it works](docs/architecture.md) · [Contributing](CONTRIBUTING.md)

CallNav is an early open source project. It records executed Python call relationships, not function values or a complete execution replay. It has a Python recorder with no runtime dependencies and a VS Code extension with no npm dependencies.

## Install and try it

Requirements: Python **3.11+** for the recorder and every Python process being recorded, VS Code **1.85+**, and your application's existing dependencies. The commands below use a POSIX shell. GPU libraries and Ray are optional; the example needs neither.

From this source checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .

callnav --version
callnav record --root "$PWD" -- bash examples/mini_pipeline/launch.sh
python vscode/scripts/package.py
```

The last command prints the generated VSIX path. In VS Code, run **Extensions: Install from VSIX…** and select that file. If the `code` command is available, you can build and install it with:

```bash
callnav_vsix="$(python vscode/scripts/package.py)"
code --install-extension "$callnav_vsix"
```

The Python package and extension are built from this checkout. These instructions do not require a PyPI or Marketplace release.

Open this checkout in VS Code:

1. Run **CallNav: Load Latest Runtime Index** from the Command Palette.
2. Run **CallNav: Enable Runtime Navigation**.
3. Open [examples/mini_pipeline/train.py](examples/mini_pipeline/train.py). Ctrl+click `reward_function` in `return reward_function(text)`: VS Code offers the two reward implementations observed in this run.
4. Click `generate_function` in `await generate_function(...)` to open the selected async plugin.

Use Cmd+click on macOS, or the navigation modifier configured in VS Code. **Go to Definition** also works. The sample runs a complete small rollout → reward → subprocess training → weight sync → cleanup flow on CPU.

## Record your own startup script

Activate the environment you normally use to run your project. Install CallNav there from your source checkout, then run from your project's working directory:

```bash
# Replace this with the location of your CallNav checkout.
python -m pip install /path/to/callnav

cd /path/to/your-project
callnav record --root "$PWD" -- bash ./launch.sh --your-option value
```

Everything after `--` is your original command and its arguments. CallNav keeps the current working directory, terminal output, and command exit status. You can also record Python entry points directly:

```bash
callnav record --root "$PWD" -- python main.py --config config.yaml
callnav record --root "$PWD" -- python -m your_package
```

Run the same small, representative workload you would normally run, including initialization, rollout and training where relevant. CallNav does not choose a generic "one training step" for arbitrary frameworks; use your application's own step/batch limits.

Open **your project folder** in VS Code and use the same two CallNav commands. No server-specific paths need to be configured. The default index is `.callnav/latest.json` under the `--root` you chose.

For Remote-SSH, record on the remote host, open that same checkout through Remote-SSH, and install the VSIX in the remote workspace extension host.

## Additional source trees and Ray

Both caller and target must belong to configured source trees. Include external checkouts when you want to navigate into them:

```bash
callnav record --root "$PWD" \
  --include-root ../external-library \
  -- bash ./launch.sh
```

For supported Ray Actor methods and constructors, opt in with `--ray`:

```bash
callnav record --ray --root "$PWD" -- python ray_main.py
```

A call such as `runner.run.remote(config)` can then point to the `run` body observed in the worker. For PATH-based, blocking `ray job submit [options] -- command` launchers, `--ray` also carries recording variables into the job's JSON runtime environment. Ray integration requires instrumented submitters and workers and shared source/recording paths. See the [usage guide](docs/usage.md#ray-and-framework-launchers) for supported launch forms and framework iteration adapters.

## What to expect

- Only executed, supported call sites have runtime destinations. Several observed targets produce several candidates.
- Files stay in ordinary **Python** mode. Pylance and other static providers continue working; VS Code may combine their definitions with CallNav's observed targets.
- Changed or unsaved source is rejected for runtime navigation. Save the matching source or record again.
- Recordings use the paths of your own checkout. Moving an existing recording to another path or machine is not supported; record again there.
- Python profiling adds overhead. Native/CUDA/JIT internals, HTTP relationships, and uninstrumented processes are outside the recorded graph. See [coverage and limitations](docs/usage.md#coverage-and-limitations).
- Recordings contain source paths and launch/process arguments. Review them before sharing; arguments can contain credentials.

Every run has its own `.callnav/<run-id>/` directory. Use **CallNav: Load Runtime Index…** to select an older `index.json`. Add `.callnav/` to your project's `.gitignore`.

## Develop

```bash
python -m pip install -e .
PYTHONPATH=src python -m unittest discover -s tests -v
node --test vscode/test/unit.test.js
python vscode/scripts/package.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and [vscode/README.md](vscode/README.md) for extension-host tests. CallNav is [MIT licensed](LICENSE).
