import importlib.util
import json
import os
import platform
import shutil
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "offline-desktop-kit" / "build_offline_desktop_kits.py"
SPEC = importlib.util.spec_from_file_location("offline_desktop_builder", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def set_nested(data, keys, value):
    current = data
    for key in keys[:-1]:
        current = current[key]
    current[keys[-1]] = value


def external_source(kit, assets, relative_path):
    for external_name, target in MODULE.SPECS[kit]["assets"].items():
        if relative_path == target:
            return assets / external_name
        prefix = target.rstrip("/") + "/"
        if relative_path.startswith(prefix):
            return assets / external_name / relative_path[len(prefix) :]
    raise AssertionError(f"no external source for {kit}:{relative_path}")


def pin_test_manifest(templates, kit, assets):
    template = templates / kit
    manifest_path = template / (
        "runtime-manifest.json" if kit == "codex-windows-runtime" else "package-manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for relative_path, keys in MODULE.HASH_BINDINGS[kit]:
        source = external_source(kit, assets, relative_path)
        set_nested(manifest, keys, MODULE.sha256_file(source))
    if kit == "codex-windows":
        set_nested(manifest, ("webview2_runtime", "sha256"), MODULE.sha256_file(assets / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"))
        set_nested(
            manifest,
            ("codex_primary_runtime", "sha256"),
            MODULE.sha256_file(assets / "codex-primary-runtime.tar.gz"),
        )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class OfflineDesktopKitTests(unittest.TestCase):
    def create_assets(self, base, kit):
        assets = base / kit / "assets"
        assets.mkdir(parents=True)
        for external_name in MODULE.SPECS[kit]["assets"]:
            path = assets / external_name
            if external_name.endswith(".app"):
                executable = path / "Contents/MacOS/claude"
                executable.parent.mkdir(parents=True)
                executable.write_bytes(f"{kit}:{external_name}".encode())
                executable.chmod(0o755)
            else:
                path.write_bytes(f"{kit}:{external_name}".encode())
        if kit == "codex-windows":
            (assets / "codex-primary-runtime.tar.gz").write_bytes(b"companion-runtime")
            (assets / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe").write_bytes(b"companion-webview")
        return assets

    def test_all_kit_layouts_build_with_pinned_external_payloads(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            templates = base / "templates"
            shutil.copytree(MODULE.TEMPLATES, templates, copy_function=shutil.copy2, symlinks=True)
            old_templates = MODULE.TEMPLATES
            MODULE.TEMPLATES = templates
            try:
                for kit in MODULE.SPECS:
                    if kit.endswith("-macos") and platform.system() != "Darwin":
                        continue
                    with self.subTest(kit=kit):
                        assets = self.create_assets(base, kit)
                        output = base / kit / "output"
                        if kit == "claude-macos":
                            link = assets / "claude-code-engine-arm64.app/Contents/Resources/claude-link"
                            link.parent.mkdir(parents=True)
                            os.symlink("../MacOS/claude", link)
                        pin_test_manifest(templates, kit, assets)
                        archive_path = MODULE.build(kit, assets, output)
                        with zipfile.ZipFile(archive_path) as archive:
                            names = archive.namelist()
                            self.assertFalse(any(name.endswith("SHA256SUMS.txt") for name in names))
                            if kit == "codex-windows":
                                self.assertFalse(any("MicrosoftEdgeWebView2" in name for name in names))
                            if kit == "codex-windows-runtime":
                                self.assertTrue(any("MicrosoftEdgeWebView2" in name for name in names))
                                self.assertFalse(any(name.endswith((".cmd", ".ps1")) for name in names))
                            self.assertTrue(any(name.endswith("THIRD_PARTY-NOTICES.md") for name in names))
                            self.assertFalse(any("__MACOSX" in name or "/._" in name for name in names))
                            if kit == "claude-macos":
                                command = archive.getinfo("Friend-Claude-macOS/START-macOS.command")
                                self.assertEqual((command.external_attr >> 16) & 0o777, 0o755)
                                link_info = archive.getinfo(
                                    "Friend-Claude-macOS/assets/claude-code-engine-arm64.app/Contents/Resources/claude-link"
                                )
                                self.assertTrue(stat.S_ISLNK(link_info.external_attr >> 16))
            finally:
                MODULE.TEMPLATES = old_templates

    def test_runtime_manifest_is_validated_and_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            templates = base / "templates"
            shutil.copytree(MODULE.TEMPLATES, templates, copy_function=shutil.copy2, symlinks=True)
            assets = self.create_assets(base, "codex-windows-runtime")
            pin_test_manifest(templates, "codex-windows-runtime", assets)
            old_templates = MODULE.TEMPLATES
            MODULE.TEMPLATES = templates
            try:
                archive_path = MODULE.build("codex-windows-runtime", assets, base / "output")
                with zipfile.ZipFile(archive_path) as archive:
                    manifest = json.loads(
                        archive.read("Friend-Codex-Windows-Offline-Runtime/runtime-manifest.json")
                    )
                    self.assertEqual(
                        manifest["sha256"],
                        MODULE.sha256_file(assets / "codex-primary-runtime.tar.gz"),
                    )
                with self.assertRaises(MODULE.BuildError):
                    MODULE.build("codex-windows-runtime", assets, base / "output")
            finally:
                MODULE.TEMPLATES = old_templates

    def test_mismatched_payload_and_repository_paths_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            assets = self.create_assets(base, "codex-windows-runtime")
            with self.assertRaises(MODULE.BuildError):
                MODULE.build("codex-windows-runtime", assets, base / "output")
            with self.assertRaises(MODULE.BuildError):
                MODULE.build(
                    "codex-windows-runtime",
                    assets,
                    MODULE.REPO_ROOT / "offline-desktop-kit/out",
                )


    def test_wrong_companion_webview_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            templates = base / "templates"
            shutil.copytree(MODULE.TEMPLATES, templates, symlinks=True)
            assets = self.create_assets(base, "codex-windows")
            pin_test_manifest(templates, "codex-windows", assets)
            (assets / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe").write_bytes(b"wrong-version")
            old_templates = MODULE.TEMPLATES
            MODULE.TEMPLATES = templates
            try:
                with self.assertRaisesRegex(MODULE.BuildError, "companion WebView2"):
                    MODULE.build("codex-windows", assets, base / "output")
            finally:
                MODULE.TEMPLATES = old_templates

    def test_missing_payload_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            with self.assertRaises(MODULE.BuildError):
                MODULE.build("claude-windows", base / "missing", base / "output")

    def test_dangling_output_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            templates = base / "templates"
            shutil.copytree(MODULE.TEMPLATES, templates, copy_function=shutil.copy2, symlinks=True)
            assets = self.create_assets(base, "codex-windows-runtime")
            pin_test_manifest(templates, "codex-windows-runtime", assets)
            output = base / "output"
            output.mkdir()
            archive = output / "Friend-Codex-Windows-Offline-Runtime-open-source-build.zip"
            escaped = base / "escaped.zip"
            try:
                os.symlink(escaped, archive)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            old_templates = MODULE.TEMPLATES
            MODULE.TEMPLATES = templates
            try:
                with self.assertRaises(MODULE.BuildError):
                    MODULE.build("codex-windows-runtime", assets, output)
                self.assertFalse(escaped.exists())
                self.assertTrue(archive.is_symlink())
            finally:
                MODULE.TEMPLATES = old_templates

    def test_templates_lock_upgrade_and_desktop_account_contracts(self):
        for kit, bindings in MODULE.HASH_BINDINGS.items():
            manifest_name = "runtime-manifest.json" if kit == "codex-windows-runtime" else "package-manifest.json"
            manifest = json.loads((MODULE.TEMPLATES / kit / manifest_name).read_text(encoding="utf-8"))
            self.assertIs(manifest["contains_real_keys"], False)
            for _, keys in bindings:
                value = manifest
                for key in keys:
                    value = value[key]
                self.assertRegex(str(value), r"^[0-9a-f]{64}$")

        windows_scripts = [
            MODULE.TEMPLATES / "claude-windows/Windows/Install-And-Start.ps1",
            MODULE.TEMPLATES / "codex-windows/Windows/Install-And-Start.ps1",
        ]
        for script in windows_scripts:
            text = script.read_text(encoding="utf-8-sig")
            self.assertIn("S-1-5-18", text)
            self.assertIn("SessionId", text)
            self.assertIn("Get-InteractiveDesktopContext", text)
            self.assertNotIn("using the current user profile", text)

        codex_windows = windows_scripts[1].read_text(encoding="utf-8-sig")
        codex_macos = (MODULE.TEMPLATES / "codex-macos/macOS/Install-And-Start.command").read_text(
            encoding="utf-8"
        )
        self.assertIn("NewerClientDetected", codex_windows)
        self.assertIn(".codex-primary-runtime.new-", codex_windows)
        self.assertLess(
            codex_windows.index("if ($NewerClientDetected)"),
            codex_windows.index("if ($existingVersion -and $existingVersion -ge $expected)"),
        )
        self.assertIn("runtime_stage", codex_macos)
        self.assertIn("newer", codex_macos.lower())
        self.assertLess(
            codex_macos.index('if version_greater "$installed_client_version"'),
            codex_macos.index('elif [[ -n "$installed_runtime" ]'),
        )

        claude_windows = windows_scripts[0].read_text(encoding="utf-8-sig")
        self.assertIn("Get-InstalledGitVersion", claude_windows)
        self.assertIn("requiredGitVersion", claude_windows)
        self.assertIn("-Verb RunAs -Wait -PassThru", claude_windows)
        self.assertIn("User.Value -ne", claude_windows)


if __name__ == "__main__":
    unittest.main()
