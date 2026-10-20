# Contributing

CallNav consists of a dependency-free Python recorder and a dependency-free
VS Code extension. Keep changes focused and preserve conservative navigation:
a runtime destination needs an observed entry and matching source fingerprints.
An unrecorded call must not become a guessed runtime edge.

## Development setup

Use Python 3.11 or newer. CI covers Python 3.11–3.13. Node.js 20 or 22 runs the extension's
unit tests. The regular checks need no GPU, Ray, framework, or editor installation.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m ruff check .
python -m unittest discover -s tests -v
node --test vscode/test/unit.test.js
```

The Ruff gate covers Python syntax, imports, and common programming errors.
Use four spaces for Python and two for JavaScript/JSON/YAML. Follow nearby code;
avoid unrelated formatting changes in a bug fix. Keep paths derived from user
arguments, environment variables, or the checkout rather than local account paths.

Add a regression test for a behavioral fix at the layer that owns it. For runtime
navigation, retain negative cases: ambiguous sites, changed or unsaved sources,
missing worker receipts, and wrappers that did not forward. Unit tests exercise
the resolver; actual editor behavior requires a genuine VS Code extension host.
The harness is `vscode/test/extension-host.js`; it is not a Node-only test.

## Optional Ray integration

The Ray backend uses private hooks and was tested with Ray 2.58.0. The opt-in test
starts an isolated local CPU cluster and checks submission-to-worker joins.
Run it only in an environment that can start Ray processes:

```bash
python -m pip install 'ray==2.58.0'
CALLNAV_TEST_RAY=1 CUDA_VISIBLE_DEVICES= python -m unittest discover -s tests -p test_ray.py -v
```

The `Checks` workflow also exposes this as a manual `ray_integration` input.
Regular pull-request checks skip the real cluster but test Ray merging and
failure cases using fixtures.

## Build and check a shareable release

```bash
python scripts/build_release.py --output dist/release
```

This prepares a clean staging tree from an explicit public-file allowlist, builds
a wheel, an sdist, a VSIX, and a standalone source archive, verifies their contents,
normalizes archive ownership metadata, and writes `SHA256SUMS`. It does not publish anything. The source archive contains
no Git history. Use that archive to prepare a new public checkout if the working
directory contains private notes or local evidence.

Local runtime recordings, training outputs, machine-specific deployment notes,
virtual environments, and validation archives are excluded without deleting
them. Adding public documentation or examples requires adding its path to both
`MANIFEST.in` and `scripts/build_release.py`. The release checker rejects absolute references to the current checkout and
build account's home directory. This is a focused guard, not a general
secret scanner; review the clean archive before sharing it.

Keep the version in `pyproject.toml`, `src/callnav/__init__.py`, and
`vscode/package.json` in sync. The builder checks this. Wheel installations
contain only the Python package and metadata; the sdist/source archive also
contain tests, examples, documentation, and extension source. The VSIX includes
only extension runtime files, README, and the MIT license.

## Changes and reports

Explain the trigger, expected behavior, and validation in a proposed change.
For a bug report, include Python/VS Code/Ray versions, a small reproducer, and
coverage warnings. Redact source paths and command arguments before sharing
recordings: an index records paths, names, fingerprints, and process metadata.

The project uses the MIT license in `LICENSE`. Contributions are provided under
that license; no contributor identity or organization is implied by the generic
copyright attribution.
