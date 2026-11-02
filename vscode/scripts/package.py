"""Build a dependency-free VSIX from an explicit extension runtime file set."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import tomllib
from xml.sax.saxutils import escape, quoteattr
import zipfile


PYTHON_RUNTIME_FILES = (
    "__init__.py", "__main__.py", "bootstrap.py", "cli.py", "collector.py", "scheduled.py",
    "callbacks.py", "deferred.py", "cache.py", "monitoring.py", "profile_monitoring.py", "hybrid_profile.py",
    "adapters/__init__.py", "adapters/ray.py", "adapters/ray_cli.py", "adapters/slime.py",
    "adapters/hydra.py",
)


def runtime_bytes(root: Path, source: Path) -> bytes:
    """Read an allowlisted file without following symlinked path components."""
    relative = source.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Refusing symlink in extension runtime: {relative}")
    if not source.is_file():
        raise ValueError(f"Missing extension runtime file: {relative}")
    return source.read_bytes()


def build(output: Path | None = None, *, root: Path | None = None) -> Path:
    root = Path(root or Path(__file__).resolve().parents[1]).absolute()
    project = root.parent
    package = json.loads(runtime_bytes(project, root / "package.json"))
    python = project / "src/callnav"
    discovered = {source.relative_to(python).as_posix() for source in python.rglob("*.py")}
    if discovered != set(PYTHON_RUNTIME_FILES):
        raise ValueError("Python recorder files differ from the explicit runtime allowlist")
    backend = {name: runtime_bytes(project, python / name) for name in PYTHON_RUNTIME_FILES}
    metadata = tomllib.loads(runtime_bytes(project, project / "pyproject.toml").decode("utf-8"))
    match = re.search(r'^__version__ = "([^"]+)"$', backend["__init__.py"].decode("utf-8"),
                      re.MULTILINE)
    if (not match or package["version"] != match.group(1) or
            package["version"] != metadata["project"]["version"]):
        raise ValueError("Python and VS Code versions must match")
    destination = output or root / f"{package['name']}-{package['version']}.vsix"
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = f'''<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id={quoteattr(package['name'])} Version={quoteattr(package['version'])} Publisher={quoteattr(package['publisher'])} />
    <DisplayName>{escape(package['displayName'])}</DisplayName>
    <Description xml:space="preserve">{escape(package['description'])}</Description>
    <Categories>Programming Languages,Other</Categories>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value={quoteattr(package['engines']['vscode'])} />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="workspace" />
    </Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code" /></Installation>
  <Dependencies />
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.License" Path="extension/LICENSE" Addressable="true" />
  </Assets>
</PackageManifest>'''
    content_types = '''<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="json" ContentType="application/json" />
  <Default Extension="js" ContentType="application/javascript" />
  <Default Extension="py" ContentType="text/x-python" />
  <Default Extension="md" ContentType="text/markdown" />
  <Default Extension="vsixmanifest" ContentType="text/xml" />
  <Override PartName="/extension/LICENSE" ContentType="text/plain" />
</Types>'''
    entries = {
        "extension.vsixmanifest": manifest.encode("utf-8"),
        "[Content_Types].xml": content_types.encode("utf-8"),
        "extension/LICENSE": runtime_bytes(project, project / "LICENSE"),
    }
    for name in ("package.json", "README.md", "language-configuration.json"):
        entries[f"extension/{name}"] = runtime_bytes(project, root / name)
    for folder, pattern in (("src", "*.js"), ("syntaxes", "*.json")):
        for source in sorted((root / folder).rglob(pattern)):
            entries[f"extension/{source.relative_to(root).as_posix()}"] = runtime_bytes(project, source)
    entries.update({f"extension/python/callnav/{name}": content for name, content in backend.items()})
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="VSIX destination (default: vscode/name-version.vsix)")
    arguments = parser.parse_args()
    print(build(arguments.output))


if __name__ == "__main__":
    main()
