#!/usr/bin/env python3
"""Build clean offline desktop-kit ZIPs from externally supplied payloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
REPO_ROOT = HERE.parent

SPECS = {
    "claude-macos": {
        "root": "Friend-Claude-macOS",
        "assets": {
            "official-client.dmg": "assets/official-client.dmg",
            "CC-Switch.dmg": "assets/CC-Switch.dmg",
            "CC-Switch-LICENSE": "THIRD_PARTY-LICENSES/CC-Switch-LICENSE",
            "claude-code-engine-arm64.app": "assets/claude-code-engine-arm64.app",
            "claude-code-engine-x64.app": "assets/claude-code-engine-x64.app",
        },
    },
    "claude-windows": {
        "root": "Friend-Claude-Windows-x64",
        "assets": {
            "official-client.msix": "assets/official-client.msix",
            "CC-Switch.zip": "assets/CC-Switch.zip",
            "CC-Switch-LICENSE": "THIRD_PARTY-LICENSES/CC-Switch-LICENSE",
            "Git-for-Windows-LICENSE.txt": "THIRD_PARTY-LICENSES/Git-for-Windows-LICENSE.txt",
            "Git-for-Windows.exe": "assets/Git-for-Windows.exe",
            "MicrosoftEdgeWebView2RuntimeInstallerX64.exe": "assets/MicrosoftEdgeWebView2RuntimeInstallerX64.exe",
            "claude-code-engine.exe": "assets/claude-code-engine.exe",
        },
    },
    "codex-macos": {
        "root": "Friend-Codex-macOS",
        "assets": {
            "official-client.dmg": "assets/official-client.dmg",
            "CC-Switch.dmg": "assets/CC-Switch.dmg",
            "CC-Switch-LICENSE": "THIRD_PARTY-LICENSES/CC-Switch-LICENSE",
            "codex-primary-runtime.tar.xz": "assets/codex-primary-runtime.tar.xz",
        },
    },
    "codex-windows": {
        "root": "Friend-Codex-Windows-x64-Base",
        "assets": {
            "official-client.msix": "assets/official-client.msix",
            "CC-Switch.zip": "assets/CC-Switch.zip",
            "CC-Switch-LICENSE": "THIRD_PARTY-LICENSES/CC-Switch-LICENSE",
            "MicrosoftEdgeWebView2RuntimeInstallerX64.exe": "assets/MicrosoftEdgeWebView2RuntimeInstallerX64.exe",
        },
    },
    "codex-windows-runtime": {
        "root": "Friend-Codex-Windows-Offline-Runtime",
        "assets": {
            "codex-primary-runtime.tar.gz": "codex-primary-runtime.tar.gz",
        },
    },
}

HASH_BINDINGS = {
    "claude-macos": [
        ("assets/official-client.dmg", ("official_installer_sha256",)),
        ("assets/CC-Switch.dmg", ("cc_switch_sha256",)),
        ("assets/claude-code-engine-arm64.app/Contents/MacOS/claude", ("claude_code_engine_arm64_sha256",)),
        ("assets/claude-code-engine-x64.app/Contents/MacOS/claude", ("claude_code_engine_x64_sha256",)),
    ],
    "claude-windows": [
        ("assets/official-client.msix", ("official_client", "sha256")),
        ("assets/claude-code-engine.exe", ("claude_code_engine", "sha256")),
        ("assets/Git-for-Windows.exe", ("git_for_windows", "sha256")),
        ("assets/MicrosoftEdgeWebView2RuntimeInstallerX64.exe", ("webview2", "sha256")),
        ("assets/CC-Switch.zip", ("cc_switch", "sha256")),
    ],
    "codex-macos": [
        ("assets/official-client.dmg", ("official_client_sha256",)),
        ("assets/CC-Switch.dmg", ("cc_switch_sha256",)),
        ("assets/codex-primary-runtime.tar.xz", ("codex_primary_runtime_sha256",)),
    ],
    "codex-windows": [
        ("assets/official-client.msix", ("official_client", "sha256")),
        ("assets/CC-Switch.zip", ("cc_switch", "sha256")),
        ("assets/MicrosoftEdgeWebView2RuntimeInstallerX64.exe", ("webview2_runtime", "sha256")),
    ],
    "codex-windows-runtime": [
        ("codex-primary-runtime.tar.gz", ("sha256",)),
    ],
}


class BuildError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_payload(source: Path, target: Path) -> None:
    if not source.exists():
        raise BuildError(f"missing external payload: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, copy_function=shutil.copy2, symlinks=True)
    else:
        shutil.copy2(source, target)


def nested_value(data: dict, keys: tuple[str, ...]) -> str:
    value = data
    for key in keys:
        value = value[key]
    return str(value).lower()


def validate_manifest(kit: str, root: Path, assets_dir: Path) -> None:
    if kit == "codex-windows-runtime":
        manifest_path = root / "runtime-manifest.json"
    else:
        manifest_path = root / "package-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("contains_real_keys") is not False:
        raise BuildError(f"template must declare contains_real_keys=false: {manifest_path}")
    for relative_path, keys in HASH_BINDINGS[kit]:
        actual = sha256_file(root / relative_path)
        expected = nested_value(manifest, keys)
        if actual != expected:
            raise BuildError(
                f"payload does not match pinned manifest ({relative_path}): expected {expected}, got {actual}"
            )
    if kit == "codex-windows":
        companion = assets_dir / "codex-primary-runtime.tar.gz"
        if not companion.is_file():
            raise BuildError(f"missing companion payload used by the Base manifest: {companion}")
        expected = nested_value(manifest, ("codex_primary_runtime", "sha256"))
        actual = sha256_file(companion)
        if actual != expected:
            raise BuildError(
                f"companion Runtime does not match Base manifest: expected {expected}, got {actual}"
            )


def write_checksums(root: Path) -> None:
    checksum_path = root / "SHA256SUMS.txt"
    rows = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p != checksum_path):
        rows.append(f"{sha256_file(path)}  ./{path.relative_to(root).as_posix()}")
    checksum_path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def write_zip(source_root: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise BuildError(f"refusing to overwrite existing output: {output}")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True) as archive:
        entries = []
        for current, dirnames, filenames in os.walk(source_root, followlinks=False):
            current_path = Path(current)
            for dirname in list(dirnames):
                candidate = current_path / dirname
                if candidate.is_symlink():
                    entries.append(candidate)
                    dirnames.remove(dirname)
            entries.extend(current_path / filename for filename in filenames)
        for path in sorted(entries):
            arcname = (Path(source_root.name) / path.relative_to(source_root)).as_posix()
            if path.is_symlink():
                info = zipfile.ZipInfo(arcname)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, os.readlink(path).encode("utf-8"))
            else:
                info = zipfile.ZipInfo.from_file(path, arcname)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (path.stat().st_mode & 0xFFFF) << 16
                archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def require_outside_repo(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise BuildError(f"{label} must be outside the source repository: {resolved}")
    return resolved


def build(kit: str, assets_dir: Path, output_dir: Path) -> Path:
    if kit not in SPECS:
        raise BuildError(f"unsupported kit: {kit}")
    spec = SPECS[kit]
    if kit.endswith("-macos") and platform.system() != "Darwin":
        raise BuildError("macOS kits must be built on macOS so executable modes and app metadata can be preserved")
    assets_dir = require_outside_repo(assets_dir, "assets directory")
    output_dir = require_outside_repo(output_dir, "output directory")
    template = TEMPLATES / kit
    if not template.is_dir():
        raise BuildError(f"missing template: {template}")
    with tempfile.TemporaryDirectory(prefix="friend-offline-kit-") as temp_dir:
        root = Path(temp_dir) / spec["root"]
        shutil.copytree(template, root, copy_function=shutil.copy2)
        for external_name, relative_target in spec["assets"].items():
            copy_payload(assets_dir / external_name, root / relative_target)
        shutil.copy2(HERE / "THIRD_PARTY-NOTICES.md", root / "THIRD_PARTY-NOTICES.md")
        validate_manifest(kit, root, assets_dir)
        write_checksums(root)
        filename = f"{spec['root']}-open-source-build.zip"
        output = output_dir / filename
        write_zip(root, output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", required=True, choices=sorted(SPECS))
    parser.add_argument("--assets-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        output = build(args.kit, args.assets_dir, args.output_dir)
    except BuildError as exc:
        parser.error(str(exc))
    print(f"built: {output}")
    print(f"sha256: {sha256_file(output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
