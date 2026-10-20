"""Build a dependency-free VSIX from an explicit extension runtime file set."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr
import zipfile


def build(output: Path | None = None) -> Path:
    root = Path(__file__).resolve().parents[1]
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
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
  <Default Extension="md" ContentType="text/markdown" />
  <Default Extension="vsixmanifest" ContentType="text/xml" />
  <Override PartName="/extension/LICENSE" ContentType="text/plain" />
</Types>'''
    entries = {
        "extension.vsixmanifest": manifest.encode("utf-8"),
        "[Content_Types].xml": content_types.encode("utf-8"),
        "extension/LICENSE": (root.parent / "LICENSE").read_bytes(),
    }
    for name in ("package.json", "README.md", "language-configuration.json"):
        entries[f"extension/{name}"] = (root / name).read_bytes()
    for folder, pattern in (("src", "*.js"), ("syntaxes", "*.json")):
        for source in sorted((root / folder).rglob(pattern)):
            if source.is_symlink():
                raise ValueError(f"Refusing symlink in extension runtime: {source}")
            entries[f"extension/{source.relative_to(root).as_posix()}"] = source.read_bytes()
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
