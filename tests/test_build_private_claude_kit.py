#!/usr/bin/env python3
"""Focused, credential-free tests for the private Claude kit builder."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build-private-claude-kit.py"
TEST_KEY = "unit-" + "private-" + "fixture"
TEST_GATEWAY = "https://example.invalid"
VALID_INSTALLER_URLS = {
    "macos": "https://downloads.claude.ai/releases/darwin/arm64/test/Claude.dmg",
    "windows": "https://downloads.claude.ai/releases/win32/x64/test/Claude.msix",
}
INSTALLER_NAMES = {"macos": "Claude.dmg", "windows": "Claude.msix"}
MODELS = ["claude-fable-5", "claude-fable-5", "claude-sonnet", "claude-sonnet"]


class BuildPrivateClaudeKitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.work = Path(self.tempdir.name)
        self.installers: dict[str, Path] = {}
        self.installer_sha256: dict[str, str] = {}
        for platform, filename in INSTALLER_NAMES.items():
            installer = self.work / f"fixture-{platform}{Path(filename).suffix}"
            installer.write_bytes(b"small fake official installer\n" * 80)
            self.installers[platform] = installer
            self.installer_sha256[platform] = hashlib.sha256(installer.read_bytes()).hexdigest()

        self.key_file = self.work / "key-input.txt"
        self.key_file.write_text(TEST_KEY, encoding="utf-8")
        self.key_file.chmod(0o600)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def builder_args(
        self,
        platform: str,
        *,
        output_dir: Path | None = None,
        installer: Path | None = None,
        key_file: Path | None = None,
        installer_url: str | None = None,
        gateway_url: str = TEST_GATEWAY,
        models: list[str] | None = None,
        version: str | None = None,
        installer_sha256: str | None = None,
        prompt_for_key: bool = False,
    ) -> list[str]:
        selected_installer = installer or self.installers[platform]
        selected_output = output_dir or self.work / "output" / platform
        selected_models = models or MODELS
        args = [
            sys.executable,
            str(BUILDER),
            "--platform",
            platform,
            "--installer",
            str(selected_installer),
            "--installer-url",
            installer_url or VALID_INSTALLER_URLS[platform],
            "--installer-sha256",
            installer_sha256 or self.installer_sha256[platform],
            "--gateway-url",
            gateway_url,
            "--output-dir",
            str(selected_output),
            "--models",
            *selected_models,
            "--quota-label",
            "unit-test-only",
            "--expires-at",
            "2099-01-01T00:00:00Z",
            "--deployment-uuid",
            "00000000-0000-0000-0000-000000000001",
            "--validation-status",
            "unit-test-only",
            "--version",
            version or f"0.1.0-{platform}",
            "--allow-small-test-installer",
        ]
        credential_args = ["--prompt-for-key"] if prompt_for_key else ["--key-file", str(key_file or self.key_file)]
        args[args.index("--gateway-url"):args.index("--gateway-url")] = credential_args
        return args

    def run_builder(self, platform: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.builder_args(platform, **kwargs),
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def build_kit(self, platform: str, **kwargs: object) -> Path:
        output_dir = kwargs.get("output_dir") or self.work / "output" / platform
        version = kwargs.get("version") or f"0.1.0-{platform}"
        result = self.run_builder(platform, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        kit = Path(output_dir) / f"Friend-Claude-{platform}-{version}-candidate.zip"
        self.assertTrue(kit.is_file(), result.stdout)
        return kit

    @staticmethod
    def read_member(archive: zipfile.ZipFile, suffix: str) -> bytes:
        matches = [info for info in archive.infolist() if info.filename.endswith(suffix)]
        if len(matches) != 1:
            raise AssertionError(f"expected one ZIP member ending in {suffix!r}: {matches}")
        return archive.read(matches[0])

    @staticmethod
    def recover_profile(script: bytes) -> dict[str, object]:
        text = script.decode("utf-8")
        marker = "PROFILE_B64='"
        start = text.find(marker)
        if start < 0:
            marker = "$PROFILE_B64 = '"
            start = text.find(marker)
        if start < 0:
            raise AssertionError("install script does not contain PROFILE_B64")
        start += len(marker)
        end = text.find("'", start)
        if end < 0:
            raise AssertionError("PROFILE_B64 is not terminated")
        raw = base64.b64decode(text[start:end], validate=True)
        profile = json.loads(raw.decode("utf-8"))
        if not isinstance(profile, dict):
            raise AssertionError("profile must be a JSON object")
        return profile

    @staticmethod
    def recover_json_variable(script: bytes, variable: str) -> dict[str, object]:
        text = script.decode("utf-8")
        markers = (f"{variable}='", f"${variable} = '")
        for marker in markers:
            start = text.find(marker)
            if start >= 0:
                start += len(marker)
                end = text.find("'", start)
                if end < 0:
                    raise AssertionError(f"{variable} is not terminated")
                encoded = text[start:end]
                if not encoded:
                    raise AssertionError(f"{variable} is empty")
                value = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
                if not isinstance(value, dict):
                    raise AssertionError(f"{variable} must decode to a JSON object")
                return value
        raise AssertionError(f"{variable} was not found")

    def test_output_zip_uses_private_host_mode(self) -> None:
        for platform in ("macos", "windows"):
            with self.subTest(platform=platform):
                kit = self.build_kit(platform, output_dir=self.work / f"mode-{platform}")
                self.assertEqual(stat.S_IMODE(kit.stat().st_mode), 0o600)

    def test_failed_zip_write_removes_partial_output(self) -> None:
        spec = importlib.util.spec_from_file_location("private_kit_builder", BUILDER)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)

        class FailingZipFile:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def __enter__(self) -> "FailingZipFile":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def writestr(self, *args: object, **kwargs: object) -> None:
                raise OSError("injected ZIP write failure")

        output_dir = self.work / "failed-write"
        parsed_args = builder.parser().parse_args(self.builder_args("macos", output_dir=output_dir)[2:])
        with mock.patch.object(builder.zipfile, "ZipFile", FailingZipFile):
            with self.assertRaises(builder.BuildError):
                builder.build(parsed_args)

        final = output_dir / "Friend-Claude-macos-0.1.0-macos-candidate.zip"
        self.assertFalse(final.exists())

    def test_official_installer_and_https_constraints(self) -> None:
        for platform, valid_url in VALID_INSTALLER_URLS.items():
            invalid_installer_urls = (
                valid_url.replace("https://", "http://"),
                valid_url.replace("downloads.claude.ai", "example.invalid"),
                f"{valid_url}?download=1",
                valid_url.replace("/test/", "/../test/"),
            )
            for index, invalid_url in enumerate(invalid_installer_urls):
                with self.subTest(platform=platform, installer_url=invalid_url):
                    result = self.run_builder(
                        platform,
                        installer_url=invalid_url,
                        output_dir=self.work / f"invalid-installer-url-{platform}-{index}",
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("installer-url is not an allowed official URL", result.stderr)

        for index, invalid_gateway in enumerate(("http://example.invalid", "https://example.invalid/path")):
            with self.subTest(gateway_url=invalid_gateway):
                result = self.run_builder(
                    "macos",
                    gateway_url=invalid_gateway,
                    output_dir=self.work / f"invalid-gateway-url-{index}",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("gateway-url must be a pure HTTPS origin", result.stderr)

    def test_installer_sha256_argument_is_strictly_validated(self) -> None:
        for raw in ("not-a-digest", "a" * 63, "g" * 64):
            with self.subTest(raw=raw):
                result = self.run_builder(
                    "macos",
                    installer_sha256=raw,
                    output_dir=self.work / f"invalid-installer-sha-{len(raw)}",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("installer-sha256 must be 64 hexadecimal characters", result.stderr)

    def test_install_scripts_bind_and_verify_official_installer(self) -> None:
        for platform in ("macos", "windows"):
            with self.subTest(platform=platform):
                kit = self.build_kit(platform, output_dir=self.work / f"installer-guard-{platform}")
                with zipfile.ZipFile(kit) as archive:
                    suffix = "Install.command" if platform == "macos" else "support/Install.ps1"
                    script = self.read_member(archive, suffix).decode("utf-8")
                expected = self.installer_sha256[platform]
                self.assertIn(expected, script)
                if platform == "macos":
                    self.assertIn("INSTALLER_SHA='" + expected + "'", script)
                    self.assertIn('[[ "$(hash_file "$DMG")" == "$INSTALLER_SHA" ]]', script)
                    self.assertLess(script.index("$INSTALLER_SHA"), script.index('mkdir -p "$CONFIG"'))
                    self.assertIn('codesign --verify --deep --strict "$APP"', script)
                    self.assertIn('spctl --assess --type execute "$APP"', script)
                    self.assertIn('[[ "$TEAM_ID" == "Q6L2SF6YDW" ]]', script)
                    self.assertLess(script.index("codesign --verify"), script.index('open "$APP"'))
                else:
                    self.assertIn("$INSTALLER_SHA = '" + expected + "'", script)
                    self.assertIn("if ((Get-Sha256 $Installer) -cne $INSTALLER_SHA)", script)
                    self.assertLess(script.index("Get-Sha256 $Installer"), script.index("Add-AppxPackage"))
                    self.assertIn("Get-AuthenticodeSignature -FilePath $Installer", script)
                    self.assertIn("$signature.Status -ne 'Valid'", script)
                    self.assertIn("$signature.SignerCertificate.Subject", script)
                    self.assertIn("*Anthropic, PBC*", script)

    def test_restore_guards_manifest_and_configuration_identity_before_delete(self) -> None:
        for platform in ("macos", "windows"):
            for prompt_for_key in (False, True):
                with self.subTest(platform=platform, prompt_for_key=prompt_for_key):
                    mode = "prompt" if prompt_for_key else "embedded"
                    kit = self.build_kit(
                        platform,
                        output_dir=self.work / f"restore-guards-{platform}-{mode}",
                        **{"prompt_for_key": prompt_for_key},
                    )
                    with zipfile.ZipFile(kit) as archive:
                        suffix = "Restore.command" if platform == "macos" else "support/Restore.ps1"
                        script = self.read_member(archive, suffix).decode("utf-8")
                    for field in ("generation_id", "deployment_uuid"):
                        self.assertIn(field, script)
                    if platform == "macos":
                        for field in (
                            'read_json_field "friend.owner" "$PROFILE"',
                            'read_json_field "friend.product" "$PROFILE"',
                            'read_json_field "friend.generation_id" "$PROFILE"',
                            'read_json_field "deploymentOrganizationUuid" "$PROFILE"',
                            'read_json_field "appliedId" "$META"',
                            'read_json_field "entries.0.id" "$META"',
                            'read_json_field "entries.0.friend_owner" "$META"',
                            'read_json_field "entries.0.friend_generation_id" "$META"',
                            'read_json_field "entries.0.product" "$META"',
                        ):
                            self.assertIn(field, script)
                        self.assertIn("plutil -extract", script)
                        self.assertLess(script.index('meta_entry_product="'), script.index('rm -f -- "$PROFILE"'))
                    else:
                        self.assertIn("ConvertFrom-Json", script)
                        for field in (
                            "$profileObject.friend.owner",
                            "$profileObject.friend.product",
                            "$profileObject.friend.generation_id",
                            "$profileObject.deploymentOrganizationUuid",
                            "$metaObject.appliedId",
                            "$metaEntry.id",
                            "$metaEntry.friend_owner",
                            "$metaEntry.friend_generation_id",
                            "$metaEntry.product",
                        ):
                            self.assertIn(field, script)
                        self.assertLess(script.index("$metaObject ="), script.index("Remove-Item -LiteralPath $Profile"))

    def test_repository_inputs_are_rejected(self) -> None:
        cases = (
            {
                "installer": ROOT / "README.md",
                "output_dir": self.work / "repo-installer",
            },
            {
                "key_file": ROOT / "README.md",
                "output_dir": self.work / "repo-key",
            },
            {
                "output_dir": ROOT / "tests",
            },
        )
        for case in cases:
            with self.subTest(case=case):
                result = self.run_builder("macos", **case)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("outside the repository", result.stderr)

    def test_platform_layout_profile_and_key_handling(self) -> None:
        expected_entries = {
            "macos": {
                "Friend-Claude-macos-0.1.0-macos-candidate/Claude.dmg",
                "Friend-Claude-macos-0.1.0-macos-candidate/Install.command",
                "Friend-Claude-macos-0.1.0-macos-candidate/Restore.command",
                "Friend-Claude-macos-0.1.0-macos-candidate/README.txt",
            },
            "windows": {
                "Friend-Claude-windows-0.1.0-windows-candidate/Claude.msix",
                "Friend-Claude-windows-0.1.0-windows-candidate/Install.cmd",
                "Friend-Claude-windows-0.1.0-windows-candidate/Restore.cmd",
                "Friend-Claude-windows-0.1.0-windows-candidate/README.txt",
                "Friend-Claude-windows-0.1.0-windows-candidate/support/Install.ps1",
                "Friend-Claude-windows-0.1.0-windows-candidate/support/Restore.ps1",
            },
        }
        expected_models = ["claude-fable-5", "claude-sonnet"]

        for platform in ("macos", "windows"):
            kit = self.build_kit(platform, output_dir=self.work / f"layout-{platform}")
            with zipfile.ZipFile(kit) as archive:
                self.assertEqual(set(archive.namelist()), expected_entries[platform])
                install_suffix = "Install.command" if platform == "macos" else "support/Install.ps1"
                install_script = self.read_member(archive, install_suffix)
                profile = self.recover_profile(install_script)
                self.assertEqual(profile["inferenceGatewayApiKey"], TEST_KEY)
                self.assertEqual(
                    [entry["name"] for entry in profile["inferenceModels"]],
                    expected_models,
                )
                manifest = self.recover_json_variable(install_script, "MANIFEST_B64")
                self.assertEqual(manifest["owner"], "friend-agent-private-kit")
                self.assertEqual(manifest["generation_id"], "friend-private-candidate-v1")
                self.assertEqual(
                    manifest["deployment_uuid"],
                    "00000000-0000-0000-0000-000000000001",
                )
                readme = self.read_member(archive, "README.txt").decode("utf-8")
                restore_suffix = "Restore.command" if platform == "macos" else "support/Restore.ps1"
                restore_script = self.read_member(archive, restore_suffix).decode("utf-8")
                self.assertIn(
                    "删除前会核对 manifest、profile/meta 身份和三个目标文件哈希，任一项不匹配就拒绝",
                    readme,
                )
                self.assertIn("动态 manifest 是防误删边界，不抵抗同一 OS 用户主动同时篡改 profile/manifest", readme)
                self.assertIn("安装失败只回滚配置；官方 Claude.app/AppX 安装可能已经完成并仍保留", readme)
                self.assertIn("Restore 不卸载 Claude.app/AppX，也不删除测试会话。", readme)
                self.assertIn("all three target files matched their package hashes", restore_script)
                if platform == "macos":
                    self.assertIn("macOS：退出 Claude 后运行 Install.command", readme)
                    self.assertNotIn("Install.cmd", readme)
                    self.assertNotIn("Restore.cmd", readme)
                    self.assertNotIn("Install.ps1", readme)
                    self.assertNotIn("Restore.ps1", readme)
                    self.assertIn('check_not_symlink "$CONFIG_PARENT" "configLibrary parent"', restore_script)
                    self.assertIn('check_not_symlink "$MANIFEST_PARENT" "manifest parent"', restore_script)
                else:
                    self.assertIn("Windows：退出 Claude 后双击 Install.cmd", readme)
                    self.assertNotIn("Install.command", readme)
                    self.assertNotIn("Restore.command", readme)
                    self.assertNotIn("Install.ps1", readme)
                    self.assertIn("Assert-NotReparse $LocalAppData 'LOCALAPPDATA'", restore_script)
                    self.assertIn("Assert-NotReparse $ConfigParent 'configLibrary parent'", restore_script)
                    self.assertIn("Assert-NotReparse $ManifestParent 'manifest parent'", restore_script)
                for info in archive.infolist():
                    self.assertNotIn(TEST_KEY, info.filename)
                    self.assertNotIn(TEST_KEY.encode("utf-8"), archive.read(info))

    def test_prompt_for_key_packages_contain_no_key_and_prompt_locally(self) -> None:
        for platform in ("macos", "windows"):
            with self.subTest(platform=platform):
                kit = self.build_kit(
                    platform,
                    output_dir=self.work / f"prompt-{platform}",
                    version=f"prompt-{platform}",
                    prompt_for_key=True,
                )
                with zipfile.ZipFile(kit) as archive:
                    install_suffix = "Install.command" if platform == "macos" else "support/Install.ps1"
                    restore_suffix = "Restore.command" if platform == "macos" else "support/Restore.ps1"
                    install_script = self.read_member(archive, install_suffix)
                    restore_script = self.read_member(archive, restore_suffix).decode("utf-8")
                    profile = self.recover_profile(install_script)
                    self.assertEqual(profile["inferenceGatewayApiKey"], "__RUNTIME_KEY__")
                    readme = self.read_member(archive, "README.txt").decode("utf-8")
                    self.assertIn("本包不包含任何 Key", readme)
                    self.assertIn("输入不回显", readme)
                    self.assertIn("同一系统用户可以读取", readme)
                    self.assertIn("generation_id", install_script.decode("utf-8"))
                    self.assertIn("deployment_uuid", install_script.decode("utf-8"))
                    self.assertNotIn(TEST_KEY.encode("utf-8"), b"".join(archive.read(info) for info in archive.infolist()))
                    self.assertIn("generation_id", restore_script)
                    self.assertIn("deployment_uuid", restore_script)
                    if platform == "macos":
                        decoded = install_script.decode("utf-8")
                        self.assertIn("read -r -s API_KEY", decoded)
                        self.assertIn('"${#API_KEY}" -ge 16', decoded)
                        self.assertIn("^[A-Za-z0-9._-]+$", decoded)
                    else:
                        decoded = install_script.decode("ascii")
                        self.assertIn("Read-Host 'Paste your gateway Key (input is hidden)' -AsSecureString", decoded)
                        self.assertIn("[A-Za-z0-9._-]{16,512}", decoded)

    def test_key_file_and_prompt_mode_are_mutually_exclusive(self) -> None:
        missing = self.builder_args("macos")
        key_index = missing.index("--key-file")
        del missing[key_index:key_index + 2]
        result = subprocess.run(missing, cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)

        both = self.builder_args("macos")
        both.insert(both.index("--gateway-url"), "--prompt-for-key")
        result = subprocess.run(both, cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS Bash and paths")
    def test_macos_prompt_install_and_restore_with_fixture_key(self) -> None:
        kit = self.build_kit(
            "macos",
            output_dir=self.work / "prompt-flow",
            version="prompt-flow",
            prompt_for_key=True,
        )
        extracted = self.work / "prompt-flow-extracted"
        with zipfile.ZipFile(kit) as archive:
            archive.extractall(extracted)
        package_root = next(extracted.iterdir())
        install = package_root / "Install.command"
        restore = package_root / "Restore.command"
        install.chmod(0o755)
        restore.chmod(0o755)

        home = self.work / "prompt-home"
        (home / "Applications" / "Claude.app").mkdir(parents=True)
        mock_bin = self.work / "prompt-mock-bin"
        mock_bin.mkdir()
        for name, body in {
            "uname": "#!/bin/sh\necho Darwin\n",
            "pgrep": "#!/bin/sh\nexit 1\n",
            "open": "#!/bin/sh\nexit 0\n",
            "codesign": "#!/bin/sh\nif [ \"$1\" = \"-dv\" ]; then echo 'TeamIdentifier=Q6L2SF6YDW' >&2; fi\nexit 0\n",
            "spctl": "#!/bin/sh\nexit 0\n",
        }.items():
            command = mock_bin / name
            command.write_text(body, encoding="utf-8")
            command.chmod(0o755)
        env = os.environ.copy()
        env.update({
            "HOME": str(home),
            "PATH": f"{mock_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": str(self.work),
        })
        fixture_key = "sk-" + "unit-" + "prompt-" + "key-" + "0001"
        installed = subprocess.run(
            ["/bin/bash", str(install)],
            input=fixture_key + "\n",
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertNotIn(fixture_key, installed.stdout + installed.stderr)
        profile_path = home / "Library" / "Application Support" / "Claude-3p" / "configLibrary" / "friend-private-candidate-v1.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        self.assertEqual(profile["inferenceGatewayApiKey"], fixture_key)
        manifest_path = home / "Library" / "Application Support" / "Friend Agent Launcher" / "private-claude-manifest-v1.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["generation_id"], "friend-private-candidate-v1")
        self.assertEqual(manifest["deployment_uuid"], "00000000-0000-0000-0000-000000000001")

        restored = subprocess.run(
            ["/bin/bash", str(restore)],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertFalse(profile_path.exists())
        self.assertTrue((home / "Applications" / "Claude.app").is_dir())

    def test_macos_install_rejects_unsafe_existing_app_paths(self) -> None:
        kit = self.build_kit("macos", output_dir=self.work / "macos-app-paths")
        with zipfile.ZipFile(kit) as archive:
            install_script = self.read_member(archive, "Install.command").decode("utf-8")

        self.assertIn(
            'for app_path in \\\n  "/Applications/Claude.app" \\\n  "$HOME/Applications/Claude.app"; do',
            install_script,
        )
        self.assertIn('[[ ! -L "$app_path" ]] || fail "Claude.app must not be a symlink"', install_script)
        self.assertIn('[[ ! -e "$app_path" || -d "$app_path" ]] || fail "Claude.app is not a directory"', install_script)
        self.assertIn('if [[ -d "/Applications/Claude.app" ]]; then', install_script)
        self.assertIn('elif [[ -d "$HOME/Applications/Claude.app" ]]; then', install_script)
        self.assertIn('[[ -d "$APP" && ! -L "$APP" ]] || fail "Claude.app is not a real directory"', install_script)
        self.assertNotIn('if [[ -e "/Applications/Claude.app" || -L "/Applications/Claude.app" ]]; then', install_script)

    def test_windows_cmd_entries_are_ascii_double_click_launchers(self) -> None:
        kit = self.build_kit("windows", output_dir=self.work / "windows-cmd")
        with zipfile.ZipFile(kit) as archive:
            install_cmd = self.read_member(archive, "/Install.cmd")
            restore_cmd = self.read_member(archive, "/Restore.cmd")

        for command, script_name in ((install_cmd, "Install.ps1"), (restore_cmd, "Restore.ps1")):
            command.decode("ascii")
            self.assertIn(b"@echo off", command)
            self.assertIn(f"%~dp0support\\{script_name}".encode("ascii"), command)
            self.assertIn(b"powershell.exe", command)
            self.assertIn(b"pause", command)
            self.assertIn(b"exit /b %EXIT_CODE%", command)

        self.assertIn(b"all three target files match their package hashes", restore_cmd)
        self.assertIn(b"does not uninstall Claude AppX or remove test sessions", restore_cmd)

    def test_existing_output_zip_is_not_overwritten(self) -> None:
        output_dir = self.work / "overwrite"
        kit = self.build_kit("macos", output_dir=output_dir, version="overwrite-test")
        original = kit.read_bytes()
        result = self.run_builder("macos", output_dir=output_dir, version="overwrite-test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("output ZIP already exists", result.stderr)
        self.assertEqual(kit.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
