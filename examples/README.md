# Examples

The CPU example needs only Python 3.11+ and CallNav. The recorder and every Python
process being recorded must use Python 3.11+. It selects functions dynamically,
runs rollout in a thread, and starts training in a child Python process.

From the CallNav checkout, after installing the package:

```bash
python -m callnav record --root examples/mini_pipeline -- \
  bash examples/mini_pipeline/launch.sh --iterations 1
```

Open `examples/mini_pipeline` in VS Code, install the CallNav extension, and run
**CallNav: Load Latest Runtime Index**, followed by **CallNav: Enable Runtime
Navigation**. Ctrl+click `generate_function` in `train.py` to jump to the observed
async implementation. Ctrl+click `reward_function` to choose between the two
reward implementations that actually ran. Use Cmd+click on macOS.

The index is written to `examples/mini_pipeline/.callnav/latest.json`. A separate
run directory preserves each recording. The launcher works from other working
directories too; pass its path and the source root relative to your current
directory, or use your own absolute paths. Set `PYTHON` if the launcher should
use a specific interpreter.

## Record your own launch script

Activate the environment that already runs your project, then install CallNav
into that environment. From your project's usual working directory:

```bash
python -m callnav record --root . -- bash ./scripts/train.sh --your-existing-argument
```

The command after `--` runs with your current working directory and arguments.
CallNav adds collection to Python processes that inherit its environment. It
does not set GPU assignments, select models or datasets, or add a training step
limit; configure those in your existing launcher.

For a VERL, Slime, or other Ray launch, add `--ray` before `--`:

```bash
python -m callnav record --root . --ray -- bash ./scripts/train.sh
```

Use `--include-root ../shared-library` for source outside the main project.
Newly started workers must inherit the recording environment. A pre-existing Ray
cluster, another container, an SSH launch, or a launcher that replaces
`PYTHONPATH` may need environment propagation; see the main
[usage guide](../README.md).

Recorded source paths identify your own checkout. They are generated during
recording; no developer server path is required. Open the same checkout in VS
Code, or use VS Code Remote SSH / Dev Containers in the environment where you
recorded.
