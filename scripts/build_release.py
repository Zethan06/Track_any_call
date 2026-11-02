"""Build shareable artifacts from a clean allowlisted tree; never publish them."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from xml.etree import ElementTree
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    ".editorconfig", ".gitattributes", ".gitignore", "CONTRIBUTING.md", "LICENSE",
    "MANIFEST.in", "README.md", "README.zh-CN.md", "pyproject.toml",
    "docs/usage.md", "docs/architecture.md", "examples/README.md",
    "scripts/build_release.py", "scripts/benchmark_recorder.py",
    "vscode/package.json", "vscode/README.md",
    "vscode/language-configuration.json", "vscode/scripts/package.py",
)
TREES = (
    ("src/callnav", "*.py"), ("tests", "*.py"),
    ("examples/mini_pipeline", "*.py"), ("examples/mini_pipeline", "*.sh"),
    ("vscode/src", "*.js"), ("vscode/test", "*.js"),
    ("vscode/syntaxes", "*.json"), (".github/workflows", "*.yml"),
)
PRIVATE_PARTS = {".git", ".callnav", ".venv", "__pycache__", "validation", "local"}
# Catch accidental references to the current build machine, independent of account.
PRIVATE_CONTENT = tuple(
    str(path).encode("utf-8") for path in {ROOT.resolve(), Path.home().resolve()}
    if str(path) != path.anchor
)
PRIVATE_FILES = {"WORKSPACE.md", "verl-ray.md", "validation.md"}


def source_bytes(root: Path, source: Path) -> bytes:
    """Read a public source without following symlinked path components."""
    relative = source.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Missing public file or symlink: {relative}")
    if not source.is_file():
        raise ValueError(f"Missing public file or symlink: {relative}")
    return source.read_bytes()


def public_files(root: Path = ROOT) -> list[Path]:
    paths = {root / name for name in FILES}
    for folder, pattern in TREES:
        paths.update(
            path for path in (root / folder).rglob(pattern)
            if not any(part in PRIVATE_PARTS for part in path.relative_to(root).parts)
        )
    for source in paths:
        relative = source.relative_to(root)
        if any(part in PRIVATE_PARTS for part in relative.parts):
            raise ValueError(f"Private path in public file list: {relative}")
        check_content(relative.as_posix(), source_bytes(root, source))
    return sorted(paths)


def check_content(name: str, content: bytes) -> None:
    path = PurePosixPath(name)
    if (path.is_absolute() or ".." in path.parts or
            any(part in PRIVATE_PARTS for part in path.parts) or path.name in PRIVATE_FILES or
            (path.name.startswith("migration-") and path.suffix == ".json")):
        raise ValueError(f"Private or invalid archive member: {name}")
    if any(marker in content for marker in PRIVATE_CONTENT):
        raise ValueError(f"Current build machine path in public file: {name}")


def version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', text, re.MULTILINE)
    if not match:
        raise ValueError("Missing project version in pyproject.toml")
    expected = match.group(1)
    package = json.loads((root / "vscode/package.json").read_text(encoding="utf-8"))
    init = (root / "src/callnav/__init__.py").read_text(encoding="utf-8")
    if package["version"] != expected or f'__version__ = "{expected}"' not in init:
        raise ValueError("Python and VS Code versions must match")
    return expected


def source_archive(root: Path, files: list[Path], target: Path, release: str) -> None:
    # Reproducible member metadata; do not preserve account names or source paths.
    with target.open("wb") as destination:
        with gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for source in files:
                    name = f"callnav-{release}/{source.relative_to(root).as_posix()}"
                    data = source.read_bytes()
                    entry = tarfile.TarInfo(name)
                    entry.size = len(data)
                    entry.mode = 0o755 if source.suffix == ".sh" else 0o644
                    entry.mtime = 0
                    archive.addfile(entry, io.BytesIO(data))


def normalize_sdist(artifact: Path) -> None:
    """Remove build-account ownership and filesystem times from an sdist."""
    temporary = artifact.with_suffix(".tmp")
    try:
        with tarfile.open(artifact, "r:gz") as source:
            with temporary.open("wb") as destination:
                with gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=0) as compressed:
                    with tarfile.open(fileobj=compressed, mode="w") as archive:
                        for member in sorted(source.getmembers(), key=lambda item: item.name):
                            if not member.isdir() and not member.isfile():
                                raise ValueError(f"Unsupported sdist member: {member.name}")
                            entry = tarfile.TarInfo(member.name)
                            entry.type = tarfile.DIRTYPE if member.isdir() else tarfile.REGTYPE
                            entry.mode = 0o755 if member.isdir() or member.name.endswith(".sh") else 0o644
                            entry.size = member.size if member.isfile() else 0
                            stream = source.extractfile(member) if member.isfile() else None
                            archive.addfile(entry, stream)
        temporary.replace(artifact)
    finally:
        temporary.unlink(missing_ok=True)


def verify_vsix(archive: zipfile.ZipFile, root: Path) -> None:
    """Require the installed extension to include the matching standalone recorder."""
    expected = {
        "extension/LICENSE": root / "LICENSE",
        **{f"extension/{name}": root / "vscode" / name
           for name in ("package.json", "README.md", "language-configuration.json")},
    }
    for folder, pattern, prefix in (
        ("vscode/src", "*.js", "extension/src"),
        ("vscode/syntaxes", "*.json", "extension/syntaxes"),
        ("src/callnav", "*.py", "extension/python/callnav"),
    ):
        directory = root / folder
        for source in directory.rglob(pattern):
            expected[f"{prefix}/{source.relative_to(directory).as_posix()}"] = source
    names = archive.namelist()
    allowed = set(expected) | {"extension.vsixmanifest", "[Content_Types].xml"}
    if len(names) != len(set(names)) or set(names) != allowed:
        raise ValueError("VSIX members differ from the explicit extension runtime allowlist")
    for name, source in expected.items():
        if archive.read(name) != source_bytes(root, source):
            raise ValueError(f"VSIX runtime does not match release source: {name}")
    release = version(root)
    package = json.loads(archive.read("extension/package.json"))
    namespace = {"vsix": "http://schemas.microsoft.com/developer/vsx-schema/2011"}
    try:
        manifest = ElementTree.fromstring(archive.read("extension.vsixmanifest"))
    except ElementTree.ParseError as error:
        raise ValueError("Invalid VSIX manifest") from error
    identity = manifest.find("vsix:Metadata/vsix:Identity", namespace)
    if identity is None or identity.get("Version") != release or package["version"] != release:
        raise ValueError("Python and VSIX manifest versions must match")


def verify_archive(artifact: Path, *, root: Path = ROOT) -> None:
    if artifact.suffix in {".whl", ".vsix"}:
        with zipfile.ZipFile(artifact) as archive:
            for info in archive.infolist():
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError(f"Link in runtime distribution: {info.filename}")
                check_content(info.filename, archive.read(info))
                if artifact.suffix == ".whl":
                    top = PurePosixPath(info.filename).parts[0]
                    if top != "callnav" and not top.endswith(".dist-info"):
                        raise ValueError(f"Unexpected wheel member: {info.filename}")
            if artifact.suffix == ".vsix":
                verify_vsix(archive, root)
    else:
        with tarfile.open(artifact, "r:gz") as archive:
            for info in archive.getmembers():
                if (info.uid, info.gid, info.uname, info.gname) != (0, 0, "", ""):
                    raise ValueError(f"Build account metadata in archive: {info.name}")
                if info.issym() or info.islnk():
                    raise ValueError(f"Link in source distribution: {info.name}")
                if info.isfile():
                    stream = archive.extractfile(info)
                    if stream is None:
                        raise ValueError(f"Unreadable archive member: {info.name}")
                    check_content(info.name, stream.read())


def build(output: Path) -> list[Path]:
    files = public_files()
    release = version(ROOT)
    names = [
        f"callnav-{release}.tar.gz", f"callnav-{release}-py3-none-any.whl",
        f"callnav-{release}.vsix", f"callnav-{release}-source.tar.gz",
    ]
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name in [*names, "SHA256SUMS"]:
        if (output / name).exists():
            raise ValueError(f"Refusing to overwrite existing release artifact: {output / name}")
    with tempfile.TemporaryDirectory(prefix="callnav-release-") as temporary:
        staging = Path(temporary) / f"callnav-{release}"
        staging.mkdir()
        for source in files:
            destination = staging / source.relative_to(ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        source_archive(staging, public_files(staging), output / names[-1], release)
        subprocess.run(
            [sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir",
             str(output), str(staging)], check=True,
        )
        subprocess.run(
            [sys.executable, str(staging / "vscode/scripts/package.py"),
             "--output", str(output / f"callnav-{release}.vsix")], check=True,
        )
    normalize_sdist(output / f"callnav-{release}.tar.gz")
    artifacts = [output / name for name in names]
    for artifact in artifacts:
        verify_archive(artifact)
    checksums = "".join(
        f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}\n"
        for artifact in artifacts
    )
    (output / "SHA256SUMS").write_text(checksums, encoding="utf-8")
    return artifacts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/release",
                        help="Output directory (existing release files are never overwritten)")
    parser.add_argument("--check", action="store_true",
                        help="Check public files and version consistency without building")
    arguments = parser.parse_args()
    try:
        if arguments.check:
            count = len(public_files())
            print(f"Public file list: {count} files; version {version(ROOT)}")
        else:
            for artifact in build(arguments.output):
                print(artifact)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Release build failed: {error}\n")


if __name__ == "__main__":
    main()
