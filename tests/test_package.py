"""The installed VSIX must contain a complete, independently runnable recorder."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


PROJECT = Path(__file__).resolve().parents[1]


def load_script(name, relative):
    spec = importlib.util.spec_from_file_location(name, PROJECT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


package = load_script("callnav_package", "vscode/scripts/package.py")
release = load_script("callnav_release", "scripts/build_release.py")


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="callnav package ")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.project = self.directory / "release source"
        self.extension = self.project / "vscode"
        self.extension.mkdir(parents=True)
        for name in ("LICENSE", "pyproject.toml"):
            shutil.copyfile(PROJECT / name, self.project / name)
        for name in ("package.json", "README.md", "language-configuration.json"):
            shutil.copyfile(PROJECT / "vscode" / name, self.extension / name)
        for name in ("src", "syntaxes"):
            shutil.copytree(PROJECT / "vscode" / name, self.extension / name)
        shutil.copytree(PROJECT / "src/callnav", self.project / "src/callnav",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    def build(self, name="callnav.vsix"):
        return package.build(self.directory / name, root=self.extension)

    def rewrite(self, artifact, transform):
        with zipfile.ZipFile(artifact) as archive:
            entries = {info.filename: archive.read(info) for info in archive.infolist()}
        transform(entries)
        with zipfile.ZipFile(artifact, "w") as archive:
            for name, content in entries.items():
                archive.writestr(name, content)

    def test_deterministic_archive_embeds_exact_python_sources(self):
        first = self.build()
        second = self.build("second.vsix")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as archive:
            backend = {name for name in archive.namelist()
                       if name.startswith("extension/python/")}
            expected = {f"extension/python/callnav/{name}"
                        for name in package.PYTHON_RUNTIME_FILES}
            self.assertEqual(backend, expected)
            for name in package.PYTHON_RUNTIME_FILES:
                self.assertEqual(archive.read(f"extension/python/callnav/{name}"),
                                 (self.project / "src/callnav" / name).read_bytes())
            self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0)
                                for info in archive.infolist()))
        release.verify_archive(first, root=self.project)

    def test_unlisted_backend_file_is_rejected(self):
        (self.project / "src/callnav/private.py").write_text("secret = True\n")
        with self.assertRaisesRegex(ValueError, "explicit runtime allowlist"):
            self.build()

    def test_runtime_file_and_parent_symlinks_are_rejected(self):
        backend = self.project / "src/callnav"
        source = backend / "collector.py"
        real = self.directory / "collector.py"
        source.replace(real)
        source.symlink_to(real)
        with self.assertRaisesRegex(ValueError, "Refusing symlink"):
            self.build()
        source.unlink()
        real.replace(source)
        moved = self.directory / "backend"
        backend.replace(moved)
        backend.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Refusing symlink"):
            self.build()

    def test_release_staging_rejects_symlinked_source_ancestor(self):
        for name in release.FILES:
            source = self.project / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(PROJECT / name, source)
        backend = self.project / "src/callnav"
        moved = self.directory / "external backend"
        backend.replace(moved)
        backend.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            release.public_files(self.project)

    def test_release_verifier_rejects_symlinked_source_ancestor(self):
        artifact = self.build()
        backend = self.project / "src/callnav"
        moved = self.directory / "external backend"
        backend.replace(moved)
        backend.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            release.verify_archive(artifact, root=self.project)

    def test_version_mismatch_is_rejected(self):
        expected = json.loads((self.extension / "package.json").read_text())["version"]
        for filename in ("src/callnav/__init__.py", "pyproject.toml"):
            with self.subTest(filename=filename):
                source = self.project / filename
                original = source.read_text(encoding="utf-8")
                source.write_text(original.replace(f'"{expected}"', '"99.0.0"'), encoding="utf-8")
                try:
                    with self.assertRaisesRegex(ValueError, "versions must match"):
                        self.build()
                finally:
                    source.write_text(original, encoding="utf-8")

    def test_release_verifier_rejects_missing_extra_or_modified_backend(self):
        changes = {
            "missing": lambda entries: entries.pop("extension/python/callnav/cli.py"),
            "extra": lambda entries: entries.update({"extension/python/private.py": b""}),
            "modified": lambda entries: entries.update({"extension/python/callnav/cli.py": b""}),
        }
        for name, transform in changes.items():
            with self.subTest(name=name):
                artifact = self.build(f"{name}.vsix")
                self.rewrite(artifact, transform)
                with self.assertRaisesRegex(ValueError, "runtime allowlist|release source"):
                    release.verify_archive(artifact, root=self.project)

    def test_release_verifier_rejects_unsynchronized_manifest_version(self):
        artifact = self.build()
        expected = json.loads((self.extension / "package.json").read_text())["version"]

        def change_version(entries):
            entries["extension.vsixmanifest"] = entries["extension.vsixmanifest"].replace(
                f'Version="{expected}"'.encode(), b'Version="99.0.0"',
            )

        self.rewrite(artifact, change_version)
        with self.assertRaisesRegex(ValueError, "manifest versions must match"):
            release.verify_archive(artifact, root=self.project)

    @unittest.skipUnless(shutil.which("bash"), "the packaged recorder shell test requires Bash")
    def test_extracted_backend_runs_without_site_packages_and_records_shell_children(self):
        installation = self.directory / "installed extension"
        with zipfile.ZipFile(self.build()) as archive:
            archive.extractall(installation)
        pythonpath = installation / "extension/python"
        working = self.directory / "unrelated working directory"
        working.mkdir()
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith("CALLNAV_")}
        environment.update(PYTHONPATH=str(pythonpath), PYTHONNOUSERSITE="1",
                           PYTHONDONTWRITEBYTECODE="1")
        probe = subprocess.run(
            [sys.executable, "-S", "-c",
             "import json, callnav; print(json.dumps([callnav.__file__, callnav.__version__]))"],
            env=environment, cwd=working, text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        origin, version = json.loads(probe.stdout)
        self.assertEqual(Path(origin), pythonpath / "callnav/__init__.py")
        self.assertEqual(version, json.loads((self.extension / "package.json").read_text())["version"])
        training = self.directory / "training project with spaces"
        training.mkdir()
        (training / "bindings.py").write_text(
            'def chosen_model():\n    return "observed"\n\n'
            'def alternate_model():\n    return "unused"\n', encoding="utf-8",
        )
        (training / "train.py").write_text(
            "import os, subprocess, sys, threading\n"
            "from bindings import chosen_model, alternate_model\n"
            "def invoke():\n"
            "    target = chosen_model if os.environ['TRAINING_BACKEND'] == 'chosen' else alternate_model\n"
            "    return target()\n"
            "assert invoke() == 'observed'\n"
            "if '--child' not in sys.argv:\n"
            "    worker = threading.Thread(target=invoke)\n"
            "    worker.start()\n"
            "    worker.join()\n"
            "    subprocess.run([sys.executable, __file__, '--child'], check=True)\n",
            encoding="utf-8",
        )
        script = training / "launch.sh"
        script.write_text('set -eu\n"$TRAINING_PYTHON" "$TRAINING_ROOT/train.py"\n', encoding="utf-8")
        output = self.directory / "runtime output"
        environment.update(TRAINING_PYTHON=sys.executable, TRAINING_ROOT=str(training),
                           TRAINING_BACKEND="chosen")
        process = subprocess.run(
            [sys.executable, "-S", "-m", "callnav", "record", "--root", str(training),
             "--output", str(output), "--", "bash", str(script)],
            cwd=working, env=environment, text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        index = json.loads((output / "index.json").read_text(encoding="utf-8"))
        selected = [edge for edge in index["edges"] if edge["target"]["qualname"] == "chosen_model"]
        self.assertEqual(sum(edge["count"] for edge in selected), 3)
        self.assertFalse(any(edge["target"]["qualname"] == "alternate_model" for edge in index["edges"]))
        self.assertGreaterEqual(len(index["processes"]), 2)
        self.assertTrue(all(item["status"] == "complete" for item in index["processes"]))
        self.assertEqual(index["warnings"], [])
        self.assertEqual(index, json.loads((training / ".callnav/latest.json").read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
