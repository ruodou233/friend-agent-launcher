#!/usr/bin/env python3
"""Runnable tests for the V1A release gates."""

from __future__ import annotations

import json
import hashlib
import io
import importlib.util
import ntpath
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
VERIFY = ROOT / "scripts" / "verify-release-support.py"
GATEWAY_VERIFY = ROOT / "scripts" / "verify-friend-gateway.py"
SCAN = ROOT / "scripts" / "scan-secrets.sh"
PREPARE = ROOT / "scripts" / "prepare-friend-test-kit.sh"
ATOMIC_PUBLISH = ROOT / "scripts" / "atomic-publish-macos.py"


def run_command(
    command: List[str], cwd: Path = ROOT, env: Optional[dict] = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def run_verify(
    *arguments: str,
    support_file: Optional[Path] = None,
    env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    command = [sys.executable, str(VERIFY)]
    if support_file is not None:
        command.extend(["--file", str(support_file)])
    command.extend(arguments)
    return run_command(command, env=env)


def git_bash_path(path: Path) -> str:
    """Pass a path to Git Bash without Windows backslashes."""

    path_text = str(path)
    drive, tail = ntpath.splitdrive(path_text)
    if (
        len(drive) == 2
        and drive[0].isalpha()
        and drive[1] == ":"
        and tail.startswith(("\\", "/"))
    ):
        normalized_tail = tail.replace("\\", "/")
        return f"/{drive[0].lower()}{normalized_tail}"
    return path.as_posix()


def git_bash_executable() -> str:
    """Return an executable for Git Bash, never WSL Bash on Windows."""

    if os.name != "nt":
        return "bash"

    candidates: List[Path] = []
    git_executable = shutil.which("git")
    if git_executable:
        git_path = Path(git_executable)
        if git_path.parent.name.lower() in {"cmd", "bin"}:
            candidates.append(git_path.parent.parent / "bin" / "bash.exe")

    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        program_files = os.environ.get(variable)
        if program_files:
            candidates.append(Path(program_files) / "Git" / "bin" / "bash.exe")

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    located_git = repr(git_executable) if git_executable else "not found"
    raise RuntimeError(
        "Git for Windows bash.exe could not be located; "
        f"shutil.which('git') returned {located_git}. "
        "Install Git for Windows or ensure its git.exe is on PATH."
    )


def run_scan(repository: Path, *artifact_paths: Path) -> subprocess.CompletedProcess:
    command = [git_bash_executable(), git_bash_path(SCAN), git_bash_path(repository)]
    command.extend(git_bash_path(path) for path in artifact_paths)
    return run_command(command)


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def create_tauri_success_bundle(bundle: Path, verifier, *, include_macos: bool = True) -> Path:
    expected_dmg = bundle / "dmg" / verifier.CANDIDATE_DMG_NAME
    write_file(expected_dmg, "claude dmg fixture")
    write_file(bundle / "dmg" / "bundle_dmg.sh", "#!/bin/sh\n")
    write_file(bundle / "dmg" / "icon.icns", "icon fixture")
    write_file(
        bundle / "share" / "create-dmg" / "support" / "template.applescript",
        "template fixture",
    )
    write_file(
        bundle / "share" / "create-dmg" / "support" / "eula-resources-template.xml",
        "eula fixture",
    )
    if include_macos:
        (bundle / "macos").mkdir(parents=True)
    return expected_dmg


def create_symlink(link: Path, target: Path, *, target_is_directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as error:
        raise unittest.SkipTest(f"symlinks are unavailable: {error}") from error


def create_fifo(path: Path) -> None:
    try:
        os.mkfifo(path)
    except (AttributeError, NotImplementedError, OSError) as error:
        raise unittest.SkipTest(f"FIFOs are unavailable: {error}") from error


def remove_tree_entry(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def load_verify_module():
    spec = importlib.util.spec_from_file_location("release_support_verify", VERIFY)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load release verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_gateway_module():
    spec = importlib.util.spec_from_file_location("friend_gateway_preflight_verify", GATEWAY_VERIFY)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load gateway preflight verifier")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeGatewayResponse:
    def __init__(self, status: int, body: bytes, headers: Optional[dict] = None):
        self.status = status
        self.body = body
        self.headers = headers or {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
        }
        self.read_limit = None
        self.read1_used = False
        self.closed = False

    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        return self.body

    def read1(self, limit: int) -> bytes:
        if self.read1_used:
            return b""
        self.read1_used = True
        return self.body[:limit]

    def close(self) -> None:
        self.closed = True


class FakeGatewayOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        if self.error is not None:
            raise self.error
        return self.response


class FakeGatewayConnection:
    def __init__(self, response):
        self.response = response
        self.request_args = None
        self.closed = False

    def request(self, method, target, headers):
        self.request_args = (method, target, headers)

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def global_dns_records(host: str, port: int, **kwargs):
    del host, kwargs
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def preflight_body(**overrides) -> bytes:
    payload = {
        "request_id": "preflight-request-1",
        "available": True,
        "product": "claude",
        "protocol": "anthropic-messages",
        "catalog_version": "v1a-test-1",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def create_mounted_dmg_fixture(root: Path, *, applications_target: str = "/Applications") -> None:
    write_file(root / ".DS_Store", "ds store")
    write_file(root / ".VolumeIcon.icns", "volume icon")
    write_file(root / "Friend Claude.app" / "Contents" / "Info.plist", "plist")
    write_file(
        root / "Friend Claude.app" / "Contents" / "MacOS" / "friend-agent-launcher",
        "binary fixture",
    )
    write_file(
        root / "Friend Claude.app" / "Contents" / "Resources" / "icon.icns",
        "icon fixture",
    )
    create_symlink(root / "Applications", Path(applications_target), target_is_directory=True)


class ReleaseSupportTests(unittest.TestCase):
    def test_git_bash_path_converts_windows_drive_absolute_path(self) -> None:
        self.assertEqual(
            git_bash_path(Path(r"D:\a\friend-agent-launcher\scripts\scan-secrets.sh")),
            "/d/a/friend-agent-launcher/scripts/scan-secrets.sh",
        )

    def test_git_bash_path_preserves_drive_relative_path(self) -> None:
        input_path = Path(r"D:relative\file")
        converted_path = git_bash_path(input_path)
        self.assertEqual(converted_path, input_path.as_posix())
        self.assertNotEqual(converted_path, "/d/relative/file")

    def test_git_bash_executable_uses_bash_on_non_windows(self) -> None:
        if os.name == "nt":
            self.skipTest("non-Windows executable selection")
        self.assertEqual(git_bash_executable(), "bash")

    def test_git_bash_path_preserves_posix_path(self) -> None:
        path = Path("/a/friend-agent-launcher/scripts/scan-secrets.sh")
        self.assertEqual(git_bash_path(path), path.as_posix())

    def test_current_matrix_is_candidate_only_and_upload_is_blocked(self) -> None:
        self.assertEqual(run_verify("--action", "check").returncode, 0)
        self.assertEqual(
            run_verify("--product", "claude", "--system", "macos", "--action", "build").returncode,
            0,
        )
        self.assertNotEqual(
            run_verify("--product", "claude", "--system", "macos", "--action", "upload").returncode,
            0,
        )
        self.assertNotEqual(
            run_verify("--product", "codex", "--system", "macos", "--action", "build").returncode,
            0,
        )

    def test_direct_candidate_build_is_blocked_by_the_wrapper_misuse_guard(self) -> None:
        environment = os.environ.copy()
        environment.pop("FRIEND_RELEASE_BUILD_WRAPPER", None)
        result = run_verify(
            "--product",
            "claude",
            "--system",
            "macos",
            "--action",
            "build",
            "--require-wrapper",
            env=environment,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("scripts/build-macos.sh", result.stderr)

    def test_wrapper_guard_is_not_described_as_a_security_boundary(self) -> None:
        verifier = (ROOT / "scripts" / "verify-release-support.py").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        implementation_plan = (ROOT / "IMPLEMENTATION_PLAN.md").read_text(encoding="utf-8")

        self.assertIn("accidental-misuse guard", verifier)
        self.assertIn("不是安全边界", readme)
        self.assertIn("不可伪造的安全边界", implementation_plan)
        for document in (verifier, readme, implementation_plan):
            self.assertNotIn("证明只能由 wrapper 构建", document)
            self.assertNotIn("安全不可绕过", document)
        self.assertNotIn("唯一允许进入 Claude macOS candidate 构建的入口", readme)

    def test_fresh_target_and_claude_only_bundle_allowlist(self) -> None:
        verifier = load_verify_module()
        macos_script = (ROOT / "scripts" / "build-macos.sh").read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            fresh_bundle = temporary_root / "fresh-target" / "release" / "bundle"
            expected_dmg = create_tauri_success_bundle(fresh_bundle, verifier)

            stale_codex = (
                temporary_root
                / "repository"
                / "src-tauri"
                / "target"
                / "release"
                / "bundle"
                / "dmg"
                / "Friend Codex_0.1.0_aarch64.dmg"
            )
            write_file(stale_codex, "stale codex fixture")

            self.assertEqual(verifier.validate_candidate_bundle(fresh_bundle), expected_dmg)

        self.assertIn("mktemp -d", macos_script)
        self.assertIn("CARGO_TARGET_DIR", macos_script)
        self.assertIn("--candidate-bundle", macos_script)
        self.assertIn("find \"$cargo_target_dir\" -depth -delete", macos_script)
        self.assertNotIn("src-tauri/target/release/bundle", macos_script)
        self.assertNotIn("rm -rf", macos_script)

    def test_candidate_bundle_accepts_missing_or_empty_macos_staging(self) -> None:
        verifier = load_verify_module()

        for include_macos in (False, True):
            with self.subTest(include_macos=include_macos), tempfile.TemporaryDirectory() as temporary:
                bundle = Path(temporary) / "target" / "release" / "bundle"
                expected_dmg = create_tauri_success_bundle(
                    bundle, verifier, include_macos=include_macos
                )

                self.assertEqual(verifier.validate_candidate_bundle(bundle), expected_dmg)
                if include_macos:
                    self.assertEqual(list((bundle / "macos").iterdir()), [])
                else:
                    self.assertFalse((bundle / "macos").exists())

    def test_candidate_bundle_allows_icon_to_be_absent(self) -> None:
        verifier = load_verify_module()

        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "target" / "release" / "bundle"
            expected_dmg = create_tauri_success_bundle(bundle, verifier)
            (bundle / "dmg" / "icon.icns").unlink()

            self.assertEqual(verifier.validate_candidate_bundle(bundle), expected_dmg)

    def test_candidate_bundle_rejects_missing_and_extra_layout_entries(self) -> None:
        verifier = load_verify_module()

        cases = (
            "missing-share",
            "missing-create-dmg",
            "missing-support",
            "missing-final-dmg",
            "missing-bundle-dmg",
            "missing-applescript",
            "missing-eula-template",
            "extra-root",
            "extra-root-directory",
            "extra-dmg-file",
            "extra-dmg-directory",
            "extra-share",
            "extra-create-dmg",
            "extra-support",
            "bundle-dmg-directory",
            "icon-directory",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                bundle = Path(temporary) / "target" / "release" / "bundle"
                create_tauri_success_bundle(bundle, verifier)
                dmg_dir = bundle / "dmg"
                share_dir = bundle / "share"
                create_dmg_dir = share_dir / "create-dmg"
                support_dir = bundle / "share" / "create-dmg" / "support"

                if case == "missing-share":
                    remove_tree_entry(share_dir)
                elif case == "missing-create-dmg":
                    remove_tree_entry(create_dmg_dir)
                elif case == "missing-support":
                    remove_tree_entry(support_dir)
                elif case == "missing-final-dmg":
                    (dmg_dir / verifier.CANDIDATE_DMG_NAME).unlink()
                elif case == "missing-bundle-dmg":
                    (dmg_dir / "bundle_dmg.sh").unlink()
                elif case == "missing-applescript":
                    (support_dir / "template.applescript").unlink()
                elif case == "missing-eula-template":
                    (support_dir / "eula-resources-template.xml").unlink()
                elif case == "extra-root":
                    write_file(bundle / "release-notes.txt", "unexpected fixture")
                elif case == "extra-root-directory":
                    (bundle / "release-notes").mkdir()
                elif case == "extra-dmg-file":
                    write_file(dmg_dir / "unexpected.txt", "unexpected fixture")
                elif case == "extra-dmg-directory":
                    (dmg_dir / "unexpected").mkdir()
                elif case == "extra-share":
                    write_file(share_dir / "unexpected.txt", "unexpected fixture")
                elif case == "extra-create-dmg":
                    write_file(create_dmg_dir / "unexpected.txt", "unexpected fixture")
                elif case == "extra-support":
                    write_file(support_dir / "unexpected.txt", "unexpected fixture")
                elif case == "bundle-dmg-directory":
                    (dmg_dir / "bundle_dmg.sh").unlink()
                    (dmg_dir / "bundle_dmg.sh").mkdir()
                else:
                    (dmg_dir / "icon.icns").unlink()
                    (dmg_dir / "icon.icns").mkdir()

                with self.assertRaises(verifier.ReleaseGateError):
                    verifier.validate_candidate_bundle(bundle)

    def test_candidate_bundle_rejects_macos_content_including_friend_claude_app(self) -> None:
        verifier = load_verify_module()

        for content in ("Friend Claude.app/Contents/Info.plist", "unexpected.txt"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                bundle = Path(temporary) / "target" / "release" / "bundle"
                create_tauri_success_bundle(bundle, verifier)
                write_file(bundle / "macos" / content, "unexpected fixture")

                with self.assertRaises(verifier.ReleaseGateError):
                    verifier.validate_candidate_bundle(bundle)

    def test_candidate_bundle_rejects_final_dmg_directory(self) -> None:
        verifier = load_verify_module()

        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "target" / "release" / "bundle"
            expected_dmg = create_tauri_success_bundle(bundle, verifier)
            expected_dmg.unlink()
            expected_dmg.mkdir()

            with self.assertRaises(verifier.ReleaseGateError):
                verifier.validate_candidate_bundle(bundle)

    def test_candidate_bundle_rejects_final_dmg_symlink(self) -> None:
        verifier = load_verify_module()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "target" / "release" / "bundle"
            expected_dmg = create_tauri_success_bundle(bundle, verifier)
            target = root / "outside.dmg"
            write_file(target, "outside dmg fixture")
            expected_dmg.unlink()
            create_symlink(expected_dmg, target, target_is_directory=False)

            with self.assertRaises(verifier.ReleaseGateError):
                verifier.validate_candidate_bundle(bundle)

    def test_candidate_bundle_rejects_final_dmg_special_file(self) -> None:
        verifier = load_verify_module()

        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "target" / "release" / "bundle"
            expected_dmg = create_tauri_success_bundle(bundle, verifier)
            expected_dmg.unlink()
            create_fifo(expected_dmg)

            with self.assertRaises(verifier.ReleaseGateError):
                verifier.validate_candidate_bundle(bundle)

    def test_candidate_bundle_rejects_root_and_share_symlinks(self) -> None:
        verifier = load_verify_module()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real_bundle = root / "real-bundle"
            create_tauri_success_bundle(real_bundle, verifier)
            root_link = root / "bundle-link"
            create_symlink(root_link, real_bundle, target_is_directory=True)
            with self.assertRaises(verifier.ReleaseGateError):
                verifier.validate_candidate_bundle(root_link)

            share_paths = (
                ("share", "share"),
                ("create-dmg", "share/create-dmg"),
                ("support", "share/create-dmg/support"),
            )
            for case, relative_path in share_paths:
                with self.subTest(case=case):
                    bundle = root / case
                    create_tauri_success_bundle(bundle, verifier)
                    link = bundle / relative_path
                    target = root / ("outside-" + case)
                    target.mkdir()
                    remove_tree_entry(link)
                    create_symlink(link, target, target_is_directory=True)

                    with self.assertRaises(verifier.ReleaseGateError):
                        verifier.validate_candidate_bundle(bundle)

    def test_candidate_bundle_rejects_share_special_file(self) -> None:
        verifier = load_verify_module()

        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "target" / "release" / "bundle"
            create_tauri_success_bundle(bundle, verifier)
            template = bundle / "share" / "create-dmg" / "support" / "template.applescript"
            template.unlink()
            create_fifo(template)

            with self.assertRaises(verifier.ReleaseGateError):
                verifier.validate_candidate_bundle(bundle)

    def test_candidate_root_entries_must_be_directories(self) -> None:
        verifier = load_verify_module()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = ("dmg", "macos", "share")
            for wrong_root_entry in cases:
                with self.subTest(wrong_root_entry=wrong_root_entry):
                    bundle = root / wrong_root_entry
                    create_tauri_success_bundle(bundle, verifier)
                    remove_tree_entry(bundle / wrong_root_entry)
                    write_file(bundle / wrong_root_entry, "not a directory")

                    with self.assertRaises(verifier.ReleaseGateError):
                        verifier.validate_candidate_bundle(bundle)

    def test_release_directory_allowlist_rejects_extra_entries(self) -> None:
        verifier = load_verify_module()
        with tempfile.TemporaryDirectory() as temporary:
            release_dir = Path(temporary) / "release" / "macos"
            write_file(release_dir / verifier.RELEASE_CANDIDATE_DMG_NAME, "candidate")
            write_file(release_dir / verifier.RELEASE_CHECKSUM_NAME, "checksum")
            verifier.validate_release_directory(release_dir, require_outputs=True)

            write_file(release_dir / "Friend Codex_0.1.0_aarch64.dmg", "codex")
            with self.assertRaises(verifier.ReleaseGateError):
                verifier.validate_release_directory(release_dir)

    def test_release_directory_allowlist_rejects_root_and_file_symlinks(self) -> None:
        verifier = load_verify_module()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real_release = root / "real-release"
            write_file(real_release / verifier.RELEASE_CANDIDATE_DMG_NAME, "candidate")
            write_file(real_release / verifier.RELEASE_CHECKSUM_NAME, "checksum")

            root_link = root / "release-link"
            create_symlink(root_link, real_release, target_is_directory=True)
            with self.assertRaises(verifier.ReleaseGateError):
                verifier.validate_release_directory(root_link, require_outputs=True)

            for name in (verifier.RELEASE_CANDIDATE_DMG_NAME, verifier.RELEASE_CHECKSUM_NAME):
                with self.subTest(name=name):
                    release_dir = root / ("file-" + name.replace("/", "-"))
                    release_dir.mkdir(parents=True)
                    target = root / ("outside-" + name)
                    write_file(target, "outside release fixture")
                    create_symlink(release_dir / name, target, target_is_directory=False)
                    other_name = (
                        verifier.RELEASE_CHECKSUM_NAME
                        if name == verifier.RELEASE_CANDIDATE_DMG_NAME
                        else verifier.RELEASE_CANDIDATE_DMG_NAME
                    )
                    write_file(release_dir / other_name, "other fixture")
                    with self.assertRaises(verifier.ReleaseGateError):
                        verifier.validate_release_directory(release_dir, require_outputs=True)

            nested = root / "nested-release"
            write_file(nested / verifier.RELEASE_CANDIDATE_DMG_NAME, "candidate")
            write_file(nested / verifier.RELEASE_CHECKSUM_NAME, "checksum")
            nested_target = root / "outside-nested"
            nested_target.mkdir(parents=True)
            nested_link = nested / "unexpected" / "link"
            nested_link.parent.mkdir(parents=True)
            create_symlink(nested_link, nested_target, target_is_directory=True)
            with self.assertRaises(verifier.ReleaseGateError):
                verifier.validate_release_directory(nested)

    def test_mounted_dmg_allowlist_accepts_current_unsigned_candidate_tree(self) -> None:
        verifier = load_verify_module()

        with tempfile.TemporaryDirectory() as temporary:
            mount_dir = Path(temporary) / "mounted-dmg"
            create_mounted_dmg_fixture(mount_dir)
            verifier.validate_mounted_dmg(mount_dir)
            result = run_verify("--action", "artifact-check", "--dmg-mount", str(mount_dir))
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_mounted_dmg_allowlist_rejects_extra_env_and_official_app(self) -> None:
        verifier = load_verify_module()

        for extra in (".env.test", "Claude.app"):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temporary:
                mount_dir = Path(temporary) / "mounted-dmg"
                create_mounted_dmg_fixture(mount_dir)
                if extra == ".env.test":
                    write_file(mount_dir / extra, "unexpected")
                else:
                    write_file(mount_dir / extra / "Contents" / "Info.plist", "unexpected")
                with self.assertRaises(verifier.ReleaseGateError):
                    verifier.validate_mounted_dmg(mount_dir)

    def test_mounted_dmg_allowlist_rejects_wrong_and_extra_symlinks(self) -> None:
        verifier = load_verify_module()

        cases = ("wrong-target", "extra-symlink")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                mount_dir = Path(temporary) / "mounted-dmg"
                create_mounted_dmg_fixture(
                    mount_dir,
                    applications_target="/tmp" if case == "wrong-target" else "/Applications",
                )
                if case == "extra-symlink":
                    create_symlink(mount_dir / "unexpected-link", Path("/Applications"), target_is_directory=True)
                with self.assertRaises(verifier.ReleaseGateError):
                    verifier.validate_mounted_dmg(mount_dir)

    def test_macho_architecture_allowlist_accepts_only_arm64(self) -> None:
        verifier = load_verify_module()
        verifier.validate_macho_archs("arm64")
        self.assertEqual(
            run_verify(
                "--action",
                "macho-archs",
                "--mach-o-archs",
                "arm64",
            ).returncode,
            0,
        )
        for archs in ("x86_64", "arm64 x86_64", "x86_64 arm64"):
            with self.subTest(archs=archs):
                with self.assertRaises(verifier.ReleaseGateError):
                    verifier.validate_macho_archs(archs)

    def test_gateway_origin_rejects_non_global_literals_and_example_domains(self) -> None:
        gateway = load_gateway_module()
        invalid_urls = (
            "http://gateway.acme",
            "https://localhost",
            "https://localhost:8443/",
            "https://127.0.0.1",
            "https://10.0.0.8",
            "https://169.254.10.2",
            "https://192.0.2.10",
            "https://::1",
            "https://[::1]",
            "https://gateway.example.com",
            "https://gateway.example.invalid",
            "https://user:password@gateway.acme",
            "https://gateway.acme?mode=test",
            "https://gateway.acme#fragment",
            "https://gateway.acme/path",
        )
        for raw_url in invalid_urls:
            with self.subTest(raw_url=raw_url):
                with self.assertRaises(gateway.GatewayOriginError):
                    gateway.validate_gateway_origin(raw_url)

    def test_gateway_preflight_uses_global_dns_https_and_no_key(self) -> None:
        gateway = load_gateway_module()
        response = FakeGatewayResponse(200, preflight_body(request_id="preflight-request-1"))
        opener = FakeGatewayOpener(response=response)
        result = gateway.verify_gateway(
            "https://gateway.acme:8443/",
            resolver=global_dns_records,
            opener=opener,
            now=datetime(2026, 8, 2, tzinfo=timezone.utc),
            request_id_factory=lambda: "preflight-request-1",
        )

        self.assertTrue(result["available"])
        self.assertEqual(opener.request.get_method(), "GET")
        self.assertIn("/v1/friend/preflight?", opener.request.full_url)
        self.assertIn("product=claude", opener.request.full_url)
        self.assertIn("protocol=anthropic-messages", opener.request.full_url)
        self.assertEqual(opener.request.get_header("X-request-id"), "preflight-request-1")
        self.assertIsNone(opener.request.data)
        self.assertIsNone(opener.request.get_header("Authorization"))
        self.assertGreater(opener.timeout, 0)
        self.assertLessEqual(opener.timeout, gateway.PREFLIGHT_TIMEOUT_SECONDS)
        self.assertEqual(response.read_limit, gateway.MAX_RESPONSE_BYTES + 1)
        self.assertTrue(response.closed)

    def test_gateway_preflight_rejects_response_request_id_mismatch(self) -> None:
        gateway = load_gateway_module()
        opener = FakeGatewayOpener(
            response=FakeGatewayResponse(200, preflight_body(request_id="another-request"))
        )
        with self.assertRaises(gateway.GatewayPreflightError):
            gateway.verify_gateway(
                "https://gateway.acme",
                resolver=global_dns_records,
                opener=opener,
                now=datetime(2026, 8, 2, tzinfo=timezone.utc),
                request_id_factory=lambda: "preflight-request-1",
            )

    def test_gateway_direct_connection_is_pinned_to_audited_ips_and_keeps_hostname(self) -> None:
        gateway = load_gateway_module()
        records = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 8443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.35", 8443)),
        ]
        calls = []
        connections = []

        def resolver(host, port, **kwargs):
            self.assertEqual(host, "gateway.acme")
            self.assertEqual(port, 8443)
            self.assertEqual(kwargs["type"], socket.SOCK_STREAM)
            return records

        def factory(origin, address, context, deadline):
            self.assertEqual(origin.hostname, "gateway.acme")
            self.assertIsNotNone(context)
            self.assertGreater(deadline, 0)
            calls.append(address)
            connection = FakeGatewayConnection(
                FakeGatewayResponse(200, preflight_body(request_id="direct-request"))
            )
            connections.append(connection)
            return connection

        with patch.dict(
            os.environ,
            {
                "HTTPS_PROXY": "https://proxy.invalid:9443",
                "HTTP_PROXY": "https://proxy.invalid:9443",
                "ALL_PROXY": "https://proxy.invalid:9443",
            },
            clear=False,
        ):
            result = gateway.verify_gateway(
                "https://gateway.acme:8443",
                resolver=resolver,
                connection_factory=factory,
                now=datetime(2026, 8, 2, tzinfo=timezone.utc),
                request_id_factory=lambda: "direct-request",
            )

        self.assertTrue(result["available"])
        self.assertEqual([address.address for address in calls], ["93.184.216.34"])
        self.assertEqual(calls[0].sockaddr, ("93.184.216.34", 8443))
        method, target, headers = connections[0].request_args
        self.assertEqual(method, "GET")
        self.assertIn("/v1/friend/preflight?", target)
        self.assertEqual(headers["Host"], "gateway.acme:8443")
        self.assertEqual(headers["X-Request-Id"], "direct-request")
        self.assertTrue(connections[0].closed)

    def test_pinned_https_connection_uses_audited_ip_and_original_sni(self) -> None:
        gateway = load_gateway_module()
        origin = gateway.validate_gateway_origin("https://gateway.acme:8443")
        address = gateway.ResolvedAddress(
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            ("93.184.216.34", 8443),
            "93.184.216.34",
        )

        class FakeSocket:
            def __init__(self):
                self.connected_to = None
                self.timeouts = []
                self.closed = False

            def settimeout(self, value):
                self.timeouts.append(value)

            def connect(self, sockaddr):
                self.connected_to = sockaddr

            def close(self):
                self.closed = True

        class FakeSecureSocket(FakeSocket):
            pass

        raw_socket = FakeSocket()
        secure_socket = FakeSecureSocket()
        context = type(
            "Context",
            (),
            {
                "verify_mode": gateway.ssl.CERT_REQUIRED,
                "check_hostname": True,
                "wrap_socket": lambda self, sock, server_hostname: (
                    self.__setattr__("wrapped", (sock, server_hostname)) or secure_socket
                )
            },
        )()
        with patch.object(gateway.socket, "socket", return_value=raw_socket):
            connection = gateway._PinnedHTTPSConnection(
                origin,
                address,
                context,
                gateway.time.monotonic() + 10,
            )
            connection.connect()

        self.assertEqual(raw_socket.connected_to, ("93.184.216.34", 8443))
        self.assertEqual(context.wrapped, (raw_socket, "gateway.acme"))
        self.assertIs(connection.sock, secure_socket)

    def test_gateway_preflight_rejects_private_dns_and_dns_failures(self) -> None:
        gateway = load_gateway_module()
        private_resolver = lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", port))
        ]
        with self.assertRaises(gateway.GatewayOriginError):
            gateway.verify_gateway(
                "https://gateway.acme",
                resolver=private_resolver,
                opener=FakeGatewayOpener(response=FakeGatewayResponse(200, preflight_body())),
            )

        def unreachable(host, port, **kwargs):
            del host, port, kwargs
            raise socket.gaierror("fixture DNS detail must not escape")

        with self.assertRaises(gateway.GatewayNetworkError):
            gateway.verify_gateway(
                "https://gateway.acme",
                resolver=unreachable,
                opener=FakeGatewayOpener(response=FakeGatewayResponse(200, preflight_body())),
            )

    def test_gateway_preflight_rejects_redirect_tls_transport_and_large_response(self) -> None:
        gateway = load_gateway_module()
        cases = (
            FakeGatewayOpener(response=FakeGatewayResponse(302, b"", {"Location": "https://other.invalid"})),
            FakeGatewayOpener(error=TimeoutError("fixture timeout detail must not escape")),
            FakeGatewayOpener(error=gateway.ssl.SSLError("fixture TLS detail must not escape")),
            FakeGatewayOpener(
                response=FakeGatewayResponse(
                    200,
                    b"x" * (gateway.MAX_RESPONSE_BYTES + 1),
                    {"Content-Type": "application/json", "Content-Length": str(gateway.MAX_RESPONSE_BYTES + 1)},
                )
            ),
        )
        for opener in cases:
            with self.subTest(opener=type(opener.response).__name__ if opener.response else "error"):
                with self.assertRaises(gateway.GatewayNetworkError):
                    gateway.verify_gateway(
                        "https://gateway.acme",
                        resolver=global_dns_records,
                        opener=opener,
                    )

    def test_gateway_preflight_strictly_validates_available_response_schema_and_expiry(self) -> None:
        gateway = load_gateway_module()
        invalid_payloads = (
            {"extra": "field"},
            {"available": False},
            {"product": "codex"},
            {"protocol": "wrong"},
            {"catalog_version": "v2-test-1"},
            {"expires_at": "2020-01-01T00:00:00Z"},
            {"expires_at": "2099-01-01T00:00:00"},
            {"request_id": "not valid"},
            {"reason_code": "CATALOG_EXPIRED"},
        )
        for override in invalid_payloads:
            with self.subTest(override=override):
                opener = FakeGatewayOpener(
                    response=FakeGatewayResponse(200, preflight_body(**override))
                )
                with self.assertRaises(gateway.GatewayPreflightError):
                    gateway.verify_gateway(
                        "https://gateway.acme",
                        resolver=global_dns_records,
                        opener=opener,
                        now=datetime(2026, 8, 2, tzinfo=timezone.utc),
                    )

    def test_gateway_preflight_cli_sanitizes_network_errors(self) -> None:
        gateway = load_gateway_module()
        output = io.StringIO()
        raw_url = "https://user:secret@gateway.acme"
        with patch.object(gateway, "verify_gateway", side_effect=gateway.GatewayNetworkError("raw detail")):
            with patch("sys.stderr", output):
                self.assertEqual(gateway.main(["--url", raw_url]), 1)
        self.assertEqual(
            output.getvalue(),
            "friend gateway preflight: BLOCKED: network/TLS/preflight verification failed\n",
        )
        self.assertNotIn(raw_url, output.getvalue())

    def test_gateway_preflight_cli_success_message_is_contract_scoped(self) -> None:
        gateway = load_gateway_module()
        output = io.StringIO()
        with patch.object(gateway, "verify_gateway", return_value={}):
            with patch("sys.stdout", output):
                self.assertEqual(gateway.main(["--url", "https://gateway.acme"]), 0)
        self.assertEqual(
            output.getvalue(),
            "friend gateway preflight: PASS: configured Claude V1A endpoint reachable and contract-matching\n",
        )
        self.assertNotIn("verified", output.getvalue().lower())
        self.assertNotIn("upstream", output.getvalue().lower())

    def test_fake_digest_and_blocked_target_cannot_upload(self) -> None:
        document = json.loads((ROOT / "release-support.json").read_text(encoding="utf-8"))
        target = next(item for item in document["targets"] if item["product"] == "claude" and item["system"] == "macos")
        target["status"] = "go"
        target["channel"] = "friend-release"
        target["p0_evidence_digest"] = "sha256:" + ("a" * 64)

        with tempfile.TemporaryDirectory() as temporary:
            support_file = Path(temporary) / "release-support.json"
            support_file.write_text(json.dumps(document), encoding="utf-8")
            allowed = run_verify(
                "--product", "claude", "--system", "macos", "--action", "upload", support_file=support_file
            )
            self.assertNotEqual(allowed.returncode, 0)

            target["p0_evidence_digest"] = None
            support_file.write_text(json.dumps(document), encoding="utf-8")
            blocked = run_verify(
                "--product", "claude", "--system", "macos", "--action", "upload", support_file=support_file
            )
            self.assertNotEqual(blocked.returncode, 0)

        blocked_target = run_verify("--product", "codex", "--system", "windows", "--action", "upload")
        self.assertNotEqual(blocked_target.returncode, 0)

    def test_future_evidence_binding_uses_exact_redacted_file_bytes(self) -> None:
        verifier = load_verify_module()
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "p0-evidence.txt"
            evidence.write_bytes(b"redacted evidence fixture\n")
            digest = "sha256:" + hashlib.sha256(evidence.read_bytes()).hexdigest()
            target = {"p0_evidence_digest": digest}
            self.assertIsNone(verifier.validate_evidence_binding(target, evidence))

            target["p0_evidence_digest"] = "sha256:" + ("a" * 64)
            with self.assertRaises(verifier.ReleaseGateError):
                verifier.validate_evidence_binding(target, evidence)

    def test_package_and_workflows_keep_release_boundaries(self) -> None:
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["author"], "ruodou233 & shing19")
        self.assertIn("ruodou233", package["contributors"])
        self.assertIn("shing19", package["contributors"])
        self.assertEqual(package["engines"]["node"], "22.x")
        cargo_toml = (ROOT / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
        self.assertIn('authors = ["ruodou233", "shing19"]', cargo_toml)
        self.assertEqual(package["scripts"]["desktop:build:claude"], "bash scripts/build-macos.sh")
        self.assertIn("process.platform", package["scripts"]["test:release"])
        self.assertIn("tests/test_release_gate.py", package["scripts"]["test:release"])
        self.assertIn("process.platform", package["scripts"]["desktop:build:codex"])
        self.assertIn("'--product','codex'", package["scripts"]["desktop:build:codex"])
        self.assertNotIn("tauri build", package["scripts"]["desktop:build:codex"])

        lock_root = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))["packages"][""]
        self.assertEqual(lock_root["author"], package["author"])
        self.assertEqual(lock_root["contributors"], package["contributors"])
        self.assertEqual(lock_root["engines"], package["engines"])
        self.assertEqual(lock_root["dependencies"], package["dependencies"])
        self.assertEqual(lock_root["devDependencies"], package["devDependencies"])

        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("FRIEND_GATEWAY_URL=https://gateway.example.invalid", env_example)
        self.assertNotIn("FRIEND_CLAUDE_MODEL", env_example)
        self.assertNotIn("FRIEND_CODEX_MODEL", env_example)

        macos_script = (ROOT / "scripts" / "build-macos.sh").read_text(encoding="utf-8")
        self.assertIn("candidate", macos_script)
        self.assertIn("scan-secrets.sh", macos_script)
        self.assertIn("FRIEND_RELEASE_BUILD_WRAPPER", macos_script)
        self.assertIn("hdiutil attach -readonly", macos_script)
        self.assertIn('[[ "$(uname -m)" == "arm64" ]]', macos_script)
        self.assertNotIn("verify-friend-gateway.py", macos_script)
        self.assertNotIn("gateway_verify_script", macos_script)
        self.assertNotIn("FRIEND_GATEWAY_URL", macos_script)
        self.assertIn("FRIEND_RELEASE_DIR", macos_script)
        self.assertIn("FRIEND_RELEASE_TEMP_ROOT", macos_script)
        self.assertIn("validate_release_boundary", macos_script)
        self.assertIn("atomic-publish-macos.py", macos_script)
        self.assertNotIn("mv -n", macos_script)
        self.assertIn("--dmg-mount", macos_script)
        self.assertIn(
            'node "$root_dir/node_modules/@tauri-apps/cli/tauri.js" build',
            macos_script,
        )
        self.assertNotIn("npm exec", macos_script)
        self.assertIn("-- --locked", macos_script)
        self.assertIn('bash "$scan_script" "$root_dir" "$mount_dir"', macos_script)
        self.assertNotIn("desktop:build:codex", macos_script)
        self.assertNotIn("desktop:build:macos", macos_script)
        self.assertNotIn("desktop:build:claude", macos_script)
        self.assertNotIn("Friend-Codex", macos_script)

        default_config = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
        self.assertFalse(default_config["bundle"]["active"])
        self.assertIn("--require-wrapper", default_config["build"]["beforeBuildCommand"])
        self.assertNotIn("desktop:build:claude", default_config["build"]["beforeBuildCommand"])

        windows_script = (ROOT / "scripts" / "build-windows.ps1").read_text(encoding="utf-8")
        self.assertIn("V1A release gate", windows_script)
        self.assertIn("Windows artifact production is blocked", windows_script)
        self.assertNotIn("npm run", windows_script)
        self.assertNotIn("Copy-Installers", windows_script)
        self.assertNotIn("Remove-Item", windows_script)

        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("node-version: 22", workflow)
        self.assertIn("actions/setup-python", workflow)
        self.assertIn("contracts/validate_contract.py", workflow)
        self.assertIn("payment-control-plane/verify.sh", workflow)
        self.assertIn("brew install ripgrep", workflow)
        self.assertIn("choco install ripgrep", workflow)
        self.assertIn("command -v rg", workflow)
        self.assertLess(
            workflow.index("- name: Ensure ripgrep is available for release scans"),
            workflow.index("- name: Run release gate tests"),
        )
        self.assertIn("tests/test_deployment_static.py", workflow)
        self.assertIn("cargo fmt", workflow)
        self.assertIn("cargo test --locked --manifest-path src-tauri/Cargo.toml --release", workflow)
        self.assertNotIn("CARGOFLAGS", workflow)
        self.assertIn("friend-gateway/tests/test_gateway.py", workflow)
        self.assertIn("scripts/scan-secrets.sh", workflow)
        self.assertIn("npm run desktop:build:macos", workflow)
        self.assertIn("Build Claude macOS local candidate", workflow)
        self.assertIn("Re-scan generated candidate paths", workflow)
        self.assertNotIn("actions/upload-artifact", workflow)
        self.assertNotIn("desktop:build:windows", workflow)
        self.assertNotIn("desktop:build:codex", workflow)

        claude_config = json.loads((ROOT / "src-tauri" / "tauri.claude.conf.json").read_text(encoding="utf-8"))
        self.assertIn("candidate", claude_config["bundle"]["longDescription"])
        self.assertTrue(claude_config["bundle"]["active"])
        self.assertEqual(claude_config["bundle"]["targets"], ["dmg"])
        self.assertIn("verify-release-support.py", claude_config["build"]["beforeBuildCommand"])
        self.assertIn("--require-wrapper", claude_config["build"]["beforeBuildCommand"])
        self.assertNotIn("一键可用", claude_config["bundle"]["longDescription"])

        codex_config = json.loads((ROOT / "src-tauri" / "tauri.codex.conf.json").read_text(encoding="utf-8"))
        self.assertFalse(codex_config["bundle"]["active"])
        self.assertIn("research", codex_config["bundle"]["shortDescription"])
        self.assertIn("blocked", codex_config["bundle"]["longDescription"])
        self.assertIn("verify-release-support.py", codex_config["build"]["beforeBuildCommand"])

    def test_contract_and_documents_keep_current_boundaries(self) -> None:
        contract_result = run_command([sys.executable, str(ROOT / "contracts" / "validate_contract.py")])
        self.assertEqual(contract_result.returncode, 0, contract_result.stderr)

        contract = json.loads((ROOT / "contracts" / "friend-api.openapi.json").read_text(encoding="utf-8"))
        schemas = contract["components"]["schemas"]
        self.assertEqual(schemas["CatalogRequest"]["required"], ["product", "protocol"])
        self.assertEqual(
            set(schemas["CatalogRequest"]["properties"]),
            {"product", "protocol"},
        )
        self.assertNotIn("account_id", schemas["CatalogRequest"]["properties"])
        self.assertNotIn("install_id", schemas["CatalogRequest"]["properties"])
        self.assertEqual(
            set(schemas["CatalogResponse"]["required"]),
            {"product", "protocol", "catalog_version", "expires_at", "integrity", "catalog", "balance"},
        )
        self.assertEqual(
            schemas["CatalogResponse"]["properties"]["integrity"]["const"],
            "tls-fixed-gateway",
        )
        self.assertNotIn("signature", json.dumps(contract).lower())
        self.assertEqual(
            schemas["BalanceSnapshot"]["properties"]["amount_minor"]["type"],
            "integer",
        )
        self.assertEqual(schemas["BalanceResponse"]["required"], ["product", "balance"])

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("非官方 companion/launcher", readme)
        self.assertIn("作者：ruodou233、shing19", readme)
        self.assertIn("Claude macOS", readme)
        self.assertIn("Codex macOS / Windows", readme)
        self.assertIn("reference/mock", readme)
        self.assertIn("真实 New API", readme)
        self.assertIn("未部署模板", readme)
        self.assertNotIn("高级设置", readme)
        self.assertNotIn("双产品可下载", readme)
        self.assertIn("配置成功后，Key 按 Claude 官方 3P 静态配置写入当前 Friend profile", readme)
        self.assertIn("本工具设置、日志和恢复 manifest 不另存或回显", readme)
        self.assertNotIn("不会回显、落盘或进入日志", readme)

        implementation_plan = (ROOT / "IMPLEMENTATION_PLAN.md").read_text(encoding="utf-8")
        self.assertIn("旧行为与已落地基线", implementation_plan)
        self.assertIn("tls-fixed-gateway", implementation_plan)
        self.assertIn("公钥签名是未来增强", implementation_plan)
        self.assertIn("当前 Claude profile", implementation_plan)
        self.assertIn("不可伪造的安全边界", implementation_plan)
        self.assertNotIn("本轮禁止修改 README", implementation_plan)

        frontend = (ROOT / "src" / "main.js").read_text(encoding="utf-8")
        self.assertIn("配置成功后 Key 会写入当前 Friend profile", frontend)
        self.assertIn("本工具设置、日志和恢复 manifest 不另存或回显", frontend)
        self.assertNotIn("不会回显、落盘或进入日志", frontend)

    def test_build_macos_publishes_only_a_validated_staging_directory(self) -> None:
        script = (ROOT / "scripts" / "build-macos.sh").read_text(encoding="utf-8")
        self.assertIn("staging_dir=$(mktemp -d \"$release_parent/.friend-release-macos.XXXXXX\")", script)
        self.assertIn('atomic_publish_script="$root_dir/scripts/atomic-publish-macos.py"', script)
        self.assertIn('python3 "$atomic_publish_script" "$staging_dir" "$release_dir"', script)
        self.assertIn("--action macho-archs", script)
        self.assertIn("/usr/bin/lipo -archs", script)
        self.assertNotIn("mv -n", script)
        self.assertNotIn("rm -rf", script)
        self.assertNotIn('rm -f "$candidate_dmg"', script)
        self.assertNotIn('rm -f "$checksum_file"', script)
        self.assertNotIn("published_candidate", script)
        self.assertNotIn("published_checksum", script)

        self.assertIn('mount_dir_identity=""', script)
        self.assertIn('mounted_identity=""', script)
        self.assertIn("file_identity()", script)
        self.assertIn("same_file_identity()", script)
        self.assertIn('staged_dmg_identity=$(file_identity "$staged_dmg")', script)
        self.assertIn('staged_checksum_identity=$(file_identity "$staged_checksum")', script)
        self.assertIn('same_directory_identity "$mount_dir" "$mounted_identity"', script)
        self.assertIn('same_directory_identity "$mount_dir" "$mount_dir_identity"', script)
        self.assertIn(
            'same_directory_identity "$mount_dir" "$mount_dir_identity" || fail "hdiutil attach 前 DMG 挂载目录 identity 不匹配"\n'
            'hdiutil attach -readonly',
            script,
        )
        self.assertNotIn("release_work_dir", script)
        self.assertEqual(script.count("trap cleanup EXIT"), 1)
        self.assertIn('tauri_frontend_dir="$cargo_target_dir/frontend"', script)
        self.assertIn('tauri_config_overlay="$cargo_target_dir/tauri.claude.overlay.json"', script)
        self.assertIn('"frontendDist": frontend_dir', script)
        self.assertIn('"beforeBuildCommand": before_build_command', script)
        self.assertIn("--outDir $frontend_dir_shell", script)
        self.assertIn('--config src-tauri/tauri.claude.conf.json', script)
        self.assertIn('--config "$tauri_config_overlay"', script)
        self.assertNotIn("dist/claude", script)
        self.assertGreaterEqual(
            script.count('same_directory_identity "$release_dir" "$staging_identity"'),
            2,
        )
        self.assertIn('verify_final_release_artifacts "release/macos 原子发布"', script)
        self.assertIn('verify_final_release_artifacts "最终 release verifier"', script)
        self.assertIn('same_file_identity "$final_dmg" "$staged_dmg_identity"', script)
        self.assertIn('same_file_identity "$final_checksum" "$staged_checksum_identity"', script)
        self.assertLess(
            script.index('verify_final_release_artifacts "release/macos 原子发布"'),
            script.index('python3 "$verify_script" \\\n  --action artifact-check \\\n  --release-dir "$release_dir"'),
        )
        self.assertLess(
            script.index('python3 "$verify_script" \\\n  --action artifact-check \\\n  --release-dir "$release_dir"'),
            script.index('verify_final_release_artifacts "最终 release verifier"'),
        )

        prepare_script = PREPARE.read_text(encoding="utf-8")
        self.assertIn("file_identity()", prepare_script)
        self.assertIn('zip_identity=$(file_identity "$zip_path")', prepare_script)
        self.assertGreaterEqual(
            prepare_script.count('same_file_identity "$final_zip" "$zip_identity"'),
            2,
        )

    def test_friend_test_kit_script_has_static_safety_contract(self) -> None:
        script = PREPARE.read_text(encoding="utf-8")

        for required in (
            "verify-friend-gateway.py",
            'python3 "$gateway_verify_script" --url "${FRIEND_GATEWAY_URL-}"',
            '[[ "$(uname -m)" == "arm64" ]]',
            'lock_dir="$dist_dir/.friend-test-kit.lock"',
            'mkdir "$lock_dir"',
            'dist_created=1',
            'staging_root=$(mktemp -d "$dist_dir/.friend-test-kit.XXXXXX")',
            'export FRIEND_RELEASE_DIR="$release_dir"',
            'export FRIEND_RELEASE_TEMP_ROOT="$temp_root"',
            'bash "$build_script"',
            "--action artifact-check",
            "--require-release-files",
            'cp -p "$candidate_dmg"',
            'cp -p "$checksum_file" "$staging_dir/$checksum_name"',
            'release_dir_identity=$(record_directory_identity "$release_dir")',
            'candidate_dmg_identity=$(file_identity "$candidate_dmg")',
            'checksum_identity=$(file_identity "$checksum_file")',
            'assert_built_release_identity "release verifier"',
            'assert_built_release_identity "读取 checksum"',
            'assert_built_release_identity "计算实际 digest"',
            'assert_built_release_identity "cp 到 staging"',
            'assert_built_checksum_content_stability "cp 到 staging"',
            'assert_built_dmg_digest "cp 到 staging"',
            'assert_staged_payload_matches_build "$staging_kit_root"',
            'python3 - "$zip_path" "$staging_kit_root"',
            'unzip -q "$zip_path" -d "$unpacked_dir"',
            'atomic_publish_script="$root_dir/scripts/atomic-publish-macos.py"',
            'python3 "$atomic_publish_script" "$zip_path" "$final_zip"',
            'unzip -q "$final_zip" -d "$post_unpacked_dir"',
            'bash "$scan_script" "$root_dir" "$staging_kit_root" "$zip_path" "$unpacked_dir/$kit_name"',
            "friend-test-kit-claude-macos-arm64-candidate.zip",
            "开始测试.md",
            "Friend Key",
            "官方 Claude Desktop",
            "恢复官方模式",
        ):
            self.assertIn(required, script)

        self.assertNotIn("npm exec", script)
        self.assertNotIn("mv -n", script)
        self.assertIn('cp -p "$checksum_file" "$staging_dir/$checksum_name"', script)
        self.assertNotIn("FRIEND_CODEX", script)
        self.assertNotIn("release-support.json", script)
        self.assertNotIn("final_kit_dir", script)
        self.assertNotIn("rm -rf", script)
        lock_position = script.index('lock_dir="$dist_dir/.friend-test-kit.lock"')
        preflight_position = script.index('python3 "$gateway_verify_script"')
        staging_position = script.index('staging_root=$(mktemp -d')
        self.assertLess(preflight_position, script.index('mktemp -d'))
        self.assertLess(preflight_position, script.index('bash "$build_script"'))
        self.assertLess(lock_position, preflight_position)
        self.assertLess(preflight_position, staging_position)

    def test_atomic_publish_rejects_existing_targets_without_entering_or_overwriting(self) -> None:
        script = ATOMIC_PUBLISH.read_text(encoding="utf-8")
        self.assertIn("RENAME_EXCL = 0x00000004", script)
        self.assertIn("RENAME_NOFOLLOW_ANY = 0x10", script)
        self.assertIn("RENAME_EXCL | RENAME_NOFOLLOW_ANY", script)
        if sys.platform != "darwin":
            raise unittest.SkipTest("macOS native rename behavior test requires Darwin")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            staging_dir = root / "staging-directory"
            staging_dir.mkdir()
            write_file(staging_dir / "payload.txt", "staging payload\n")
            existing_directory = root / "external-target-directory"
            self.assertFalse(existing_directory.exists())
            # Model the external actor winning the check-to-publish race.
            write_file(existing_directory / "sentinel.txt", "external sentinel\n")

            directory_result = run_command(
                [sys.executable, str(ATOMIC_PUBLISH), str(staging_dir), str(existing_directory)]
            )
            self.assertNotEqual(directory_result.returncode, 0)
            self.assertIn("target already exists", directory_result.stderr)
            self.assertNotIn(str(staging_dir), directory_result.stdout + directory_result.stderr)
            self.assertNotIn(str(existing_directory), directory_result.stdout + directory_result.stderr)
            self.assertTrue(staging_dir.is_dir())
            self.assertEqual(
                (existing_directory / "sentinel.txt").read_text(encoding="utf-8"),
                "external sentinel\n",
            )
            self.assertFalse((existing_directory / staging_dir.name).exists())

            staging_zip = root / "staging.zip"
            write_file(staging_zip, "staging zip\n")
            existing_zip_directory = root / "external-target-zip-directory"
            self.assertFalse(existing_zip_directory.exists())
            write_file(existing_zip_directory / "sentinel.txt", "external zip sentinel\n")

            zip_result = run_command(
                [sys.executable, str(ATOMIC_PUBLISH), str(staging_zip), str(existing_zip_directory)]
            )
            self.assertNotEqual(zip_result.returncode, 0)
            self.assertIn("target already exists", zip_result.stderr)
            self.assertTrue(staging_zip.is_file())
            self.assertEqual(
                (existing_zip_directory / "sentinel.txt").read_text(encoding="utf-8"),
                "external zip sentinel\n",
            )
            self.assertFalse((existing_zip_directory / staging_zip.name).exists())

    def test_atomic_publish_performs_an_atomic_successful_rename(self) -> None:
        if sys.platform != "darwin":
            raise unittest.SkipTest("macOS native rename behavior test requires Darwin")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "staging-directory"
            destination = root / "published-directory"
            write_file(source / "payload.txt", "published payload\n")

            result = run_command([sys.executable, str(ATOMIC_PUBLISH), str(source), str(destination)])

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(source.exists())
            self.assertTrue(destination.is_dir())
            self.assertEqual(
                (destination / "payload.txt").read_text(encoding="utf-8"),
                "published payload\n",
            )
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")

    def test_atomic_publish_rejects_symlinked_parent_without_mutating_source_or_target(self) -> None:
        if sys.platform != "darwin":
            raise unittest.SkipTest("macOS native rename symlink behavior test requires Darwin")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "staging-directory"
            write_file(source / "payload.txt", "staging payload\n")
            external_parent = root / "external-parent"
            write_file(external_parent / "sentinel.txt", "external sentinel\n")
            linked_parent = root / "linked-parent"
            create_symlink(linked_parent, external_parent, target_is_directory=True)
            destination = linked_parent / "published-directory"

            result = run_command(
                [sys.executable, str(ATOMIC_PUBLISH), str(source), str(destination)]
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(str(source), result.stdout + result.stderr)
            self.assertNotIn(str(destination), result.stdout + result.stderr)
            self.assertTrue(source.is_dir())
            self.assertEqual(
                (source / "payload.txt").read_text(encoding="utf-8"),
                "staging payload\n",
            )
            self.assertEqual(
                (external_parent / "sentinel.txt").read_text(encoding="utf-8"),
                "external sentinel\n",
            )
            self.assertFalse((external_parent / "published-directory").exists())

    def test_friend_test_kit_rejects_invalid_gateway_urls_before_build(self) -> None:
        invalid_urls = (
            None,
            "http://gateway.acme",
            "https://localhost",
            "https://localhost:8443/",
            "https://127.0.0.1",
            "https://example.invalid",
            "https://gateway.example.com",
            "https://user:password@gateway.acme",
            "https://gateway.acme?mode=test",
            "https://gateway.acme#fragment",
            "https://gateway.acme/path",
            "https://gateway.acme/extra/",
        )

        command = (
            [git_bash_executable(), git_bash_path(PREPARE)]
            if os.name == "nt"
            else ["bash", str(PREPARE)]
        )
        for invalid_url in invalid_urls:
            with self.subTest(invalid_url=invalid_url):
                environment = os.environ.copy()
                if invalid_url is None:
                    environment.pop("FRIEND_GATEWAY_URL", None)
                else:
                    environment["FRIEND_GATEWAY_URL"] = invalid_url
                result = run_command(command, env=environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("朋友现场测试套件: BLOCKED:", result.stderr)
                if invalid_url:
                    self.assertNotIn(invalid_url, result.stderr)
                self.assertNotIn("build-macos.sh", result.stderr)

    def test_friend_test_kit_cleans_dist_created_for_failed_preflight(self) -> None:
        self._require_friend_kit_tools()

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            self._create_prepare_fixture(fixture)
            environment = os.environ.copy()
            environment["FRIEND_GATEWAY_URL"] = "http://gateway.acme"
            environment["FRIEND_PREFLIGHT_FAIL"] = "1"
            result = run_command(
                ["bash", str(fixture / "scripts" / "prepare-friend-test-kit.sh")],
                cwd=fixture,
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((fixture / "dist").exists())

    def test_friend_test_kit_fixture_packages_only_allowed_files(self) -> None:
        self._require_friend_kit_tools()

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            temp_dir = self._create_prepare_fixture(fixture)
            environment = os.environ.copy()
            environment["FRIEND_GATEWAY_URL"] = "https://gateway.acme"
            environment["TMPDIR"] = str(temp_dir)

            result = run_command(
                ["bash", str(fixture / "scripts" / "prepare-friend-test-kit.sh")],
                cwd=fixture,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("朋友现场测试套件: PASS:", result.stdout)
            self.assertFalse((fixture / "release" / "macos").exists())
            zip_path = fixture / "dist" / "friend-test-kit-claude-macos-arm64-candidate.zip"
            self.assertTrue(zip_path.is_file())
            self.assertFalse((fixture / "dist" / "friend-test-kit").exists())
            self.assertEqual(
                {path.name for path in (fixture / "dist").iterdir()},
                {zip_path.name},
            )

            extracted = Path(temporary) / "final-zip-check"
            with zipfile.ZipFile(zip_path) as archive:
                archive_files = {
                    name
                    for name in archive.namelist()
                    if not name.endswith("/")
                }
                archive.extractall(extracted)
            self.assertEqual(
                archive_files,
                {
                    "friend-test-kit/claude-macos/Friend-Claude-0.1.0-macos-arm64-candidate.dmg",
                    "friend-test-kit/claude-macos/SHA256SUMS-candidate.txt",
                    "friend-test-kit/claude-macos/开始测试.md",
                },
            )
            self.assertNotIn(".env", "\n".join(archive_files))
            self.assertNotIn(".log", "\n".join(archive_files))
            kit_dir = extracted / "friend-test-kit" / "claude-macos"
            expected_files = {
                "Friend-Claude-0.1.0-macos-arm64-candidate.dmg",
                "SHA256SUMS-candidate.txt",
                "开始测试.md",
            }
            self.assertEqual({path.name for path in kit_dir.iterdir()}, expected_files)
            dmg_path = kit_dir / "Friend-Claude-0.1.0-macos-arm64-candidate.dmg"
            checksum_path = kit_dir / "SHA256SUMS-candidate.txt"
            self.assertTrue(dmg_path.is_file())
            self.assertTrue(checksum_path.is_file())
            self.assertEqual(
                checksum_path.read_text(encoding="utf-8").split(),
                [hashlib.sha256(dmg_path.read_bytes()).hexdigest(), dmg_path.name],
            )
            expected_fixture_digest = hashlib.sha256(b"fixture dmg\n").hexdigest()
            self.assertEqual(
                checksum_path.read_text(encoding="utf-8"),
                f"{expected_fixture_digest}  {dmg_path.name}\n",
            )

            final_scan = run_scan(fixture, zip_path, extracted / "friend-test-kit")
            self.assertEqual(final_scan.returncode, 0, final_scan.stderr)
            self.assertIn("release scan: PASS:", final_scan.stdout)

    def test_friend_test_kit_rejects_build_file_replacement_after_release_verifier(self) -> None:
        self._require_friend_kit_tools()

        targets = (
            ("candidate DMG", "Friend-Claude-0.1.0-macos-arm64-candidate.dmg", "candidate DMG identity"),
            ("checksum", "SHA256SUMS-candidate.txt", "checksum identity"),
        )
        for label, target_name, error_fragment in targets:
            with self.subTest(target=label), tempfile.TemporaryDirectory() as temporary:
                fixture = Path(temporary) / "fixture"
                self._create_prepare_fixture(fixture)
                write_file(
                    fixture / "scripts" / "verify-release-support.py",
                    "import sys\n"
                    "from pathlib import Path\n"
                    "release_dir = Path(sys.argv[sys.argv.index('--release-dir') + 1])\n"
                    f"target = release_dir / {target_name!r}\n"
                    "target.unlink()\n"
                    "target.write_bytes(b'replaced after release verifier\\n')\n",
                )
                environment = os.environ.copy()
                environment["FRIEND_GATEWAY_URL"] = "https://gateway.acme"
                result = run_command(
                    ["bash", str(fixture / "scripts" / "prepare-friend-test-kit.sh")],
                    cwd=fixture,
                    env=environment,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error_fragment, result.stderr)
                self.assertFalse(
                    (fixture / "dist" / "friend-test-kit-claude-macos-arm64-candidate.zip").exists()
                )

    def test_friend_test_kit_binds_checksum_contents_even_when_inode_is_unchanged(self) -> None:
        self._require_friend_kit_tools()

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            self._create_prepare_fixture(fixture)
            write_file(
                fixture / "scripts" / "verify-release-support.py",
                "import sys\n"
                "from pathlib import Path\n"
                "release_dir = Path(sys.argv[sys.argv.index('--release-dir') + 1])\n"
                "(release_dir / 'SHA256SUMS-candidate.txt').write_text(\n"
                "    '0' * 64 + '  Friend-Claude-0.1.0-macos-arm64-candidate.dmg\\n',\n"
                "    encoding='utf-8',\n"
                ")\n",
            )
            environment = os.environ.copy()
            environment["FRIEND_GATEWAY_URL"] = "https://gateway.acme"
            result = run_command(
                ["bash", str(fixture / "scripts" / "prepare-friend-test-kit.sh")],
                cwd=fixture,
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("checksum 内容已变化", result.stderr)
            self.assertFalse(
                (fixture / "dist" / "friend-test-kit-claude-macos-arm64-candidate.zip").exists()
            )

    def test_friend_test_kit_rechecks_build_identity_after_copy_to_staging(self) -> None:
        self._require_friend_kit_tools()

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            self._create_prepare_fixture(fixture)
            fake_bin = Path(temporary) / "fake-bin"
            write_file(
                fake_bin / "cp",
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "/bin/cp \"$@\"\n"
                "if [[ \"${2-}\" == *Friend-Claude-0.1.0-macos-arm64-candidate.dmg ]]; then\n"
                "  rm \"$2\"\n"
                "  printf 'replaced after cp\\n' >\"$2\"\n"
                "fi\n",
            )
            (fake_bin / "cp").chmod(0o755)
            environment = os.environ.copy()
            environment["FRIEND_GATEWAY_URL"] = "https://gateway.acme"
            environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]
            result = run_command(
                ["bash", str(fixture / "scripts" / "prepare-friend-test-kit.sh")],
                cwd=fixture,
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("candidate DMG identity", result.stderr)
            self.assertFalse(
                (fixture / "dist" / "friend-test-kit-claude-macos-arm64-candidate.zip").exists()
            )

    def test_friend_test_kit_fixture_rejects_extra_release_artifact(self) -> None:
        self._require_friend_kit_tools()

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            temp_dir = self._create_prepare_fixture(fixture, extra_release_file=True)
            environment = os.environ.copy()
            environment["FRIEND_GATEWAY_URL"] = "https://gateway.acme"
            environment["TMPDIR"] = str(temp_dir)

            result = run_command(
                ["bash", str(fixture / "scripts" / "prepare-friend-test-kit.sh")],
                cwd=fixture,
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("release gate: BLOCKED:", result.stderr)
            self.assertFalse((fixture / "dist" / "friend-test-kit").exists())
            self.assertFalse(
                (fixture / "dist" / "friend-test-kit-claude-macos-arm64-candidate.zip").exists()
            )
            self.assertEqual(
                [path for path in temp_dir.iterdir() if path.name.startswith("friend-test-kit.")],
                [],
            )
            self.assertFalse((fixture / "release" / "macos").exists())

    def test_friend_test_kit_preserves_external_zip_that_appears_before_publish(self) -> None:
        self._require_friend_kit_tools()

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            self._create_prepare_fixture(fixture)
            scan_script = fixture / "scripts" / "scan-secrets.sh"
            write_file(
                scan_script,
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "printf 'external replacement\\n' >\"$1/dist/friend-test-kit-claude-macos-arm64-candidate.zip\"\n",
            )
            scan_script.chmod(0o755)
            environment = os.environ.copy()
            environment["FRIEND_GATEWAY_URL"] = "https://gateway.acme"

            result = run_command(
                ["bash", str(fixture / "scripts" / "prepare-friend-test-kit.sh")],
                cwd=fixture,
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0)
            final_zip = fixture / "dist" / "friend-test-kit-claude-macos-arm64-candidate.zip"
            self.assertEqual(final_zip.read_text(encoding="utf-8"), "external replacement\n")
            self.assertFalse((fixture / "dist" / ".friend-test-kit.lock").exists())
            self.assertEqual(
                [path.name for path in (fixture / "dist").iterdir()],
                [final_zip.name],
            )

    def test_friend_test_kit_rechecks_published_zip_with_second_scan(self) -> None:
        self._require_friend_kit_tools()

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            self._create_prepare_fixture(fixture)
            scan_script = fixture / "scripts" / "scan-secrets.sh"
            real_scan = fixture / "scripts" / "scan-secrets-real.sh"
            shutil.copy2(scan_script, real_scan)
            write_file(
                scan_script,
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "printf '%s\\n' \"$*\" >>\"$FRIEND_SCAN_LOG\"\n"
                "bash \"$(dirname \"$0\")/scan-secrets-real.sh\" \"$@\"\n",
            )
            scan_script.chmod(0o755)
            scan_log = fixture / "scan-events.txt"
            environment = os.environ.copy()
            environment["FRIEND_GATEWAY_URL"] = "https://gateway.acme"
            environment["FRIEND_SCAN_LOG"] = str(scan_log)

            result = run_command(
                ["bash", str(fixture / "scripts" / "prepare-friend-test-kit.sh")],
                cwd=fixture,
                env=environment,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            scan_calls = scan_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(scan_calls), 2)
            self.assertIn(".friend-test-kit.", scan_calls[0])
            self.assertIn(
                "friend-test-kit-claude-macos-arm64-candidate.zip",
                scan_calls[1],
            )

    def test_friend_test_kit_existing_package_fails_before_preflight_and_build(self) -> None:
        self._require_friend_kit_tools()

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            self._create_prepare_fixture(fixture)
            existing_kit = fixture / "dist" / "friend-test-kit"
            existing_zip = fixture / "dist" / "friend-test-kit-claude-macos-arm64-candidate.zip"
            write_file(existing_kit / "sentinel.txt", "old kit")
            write_file(existing_zip, "old zip")
            environment = os.environ.copy()
            environment["FRIEND_GATEWAY_URL"] = "https://gateway.acme"
            environment["FRIEND_PREFLIGHT_MARKER"] = str(fixture / "preflight-called")
            environment["FRIEND_BUILD_MARKER"] = str(fixture / "build-called")

            result = run_command(
                ["bash", str(fixture / "scripts" / "prepare-friend-test-kit.sh")],
                cwd=fixture,
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("拒绝覆盖旧包", result.stderr)
            self.assertEqual((existing_kit / "sentinel.txt").read_text(encoding="utf-8"), "old kit")
            self.assertEqual(existing_zip.read_text(encoding="utf-8"), "old zip")
            self.assertFalse((fixture / "preflight-called").exists())
            self.assertFalse((fixture / "build-called").exists())

    def test_friend_test_kit_rejects_dist_lock_symlink(self) -> None:
        self._require_friend_kit_tools()

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            self._create_prepare_fixture(fixture)
            dist_dir = fixture / "dist"
            dist_dir.mkdir()
            outside = Path(temporary) / "outside-lock"
            outside.mkdir()
            create_symlink(dist_dir / ".friend-test-kit.lock", outside, target_is_directory=True)
            environment = os.environ.copy()
            environment["FRIEND_GATEWAY_URL"] = "https://gateway.acme"
            result = run_command(
                ["bash", str(fixture / "scripts" / "prepare-friend-test-kit.sh")],
                cwd=fixture,
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("dist/.friend-test-kit.lock 不得是 symlink", result.stderr)
            self.assertFalse((fixture / "build-called").exists())

    def test_build_macos_rejects_untrusted_release_override_before_build(self) -> None:
        if sys.platform != "darwin" or platform.machine() != "arm64":
            raise unittest.SkipTest("build-macos boundary test requires Darwin arm64")

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            scripts = fixture / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(ROOT / "scripts" / "build-macos.sh", scripts / "build-macos.sh")
            shutil.copy2(ATOMIC_PUBLISH, scripts / ATOMIC_PUBLISH.name)
            write_file(scripts / "verify-release-support.py", "raise SystemExit(99)\n")
            write_file(scripts / "scan-secrets.sh", "#!/usr/bin/env bash\nexit 99\n")
            (scripts / "scan-secrets.sh").chmod(0o755)
            fake_bin = fixture / "fake-bin"
            fake_bin.mkdir()
            write_file(fake_bin / "node", "#!/usr/bin/env bash\nprintf 'v22.0.0\\n'\n")
            (fake_bin / "node").chmod(0o755)

            arbitrary_release = fixture / "arbitrary-release"
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]
            environment["FRIEND_RELEASE_DIR"] = str(arbitrary_release)
            environment.pop("FRIEND_RELEASE_TEMP_ROOT", None)
            result = run_command(
                ["bash", str(scripts / "build-macos.sh")],
                cwd=fixture,
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("FRIEND_RELEASE_DIR 只允许由 prepare 提供", result.stderr)
            self.assertFalse(arbitrary_release.exists())

    def test_build_macos_preserves_existing_release_target_without_building(self) -> None:
        if sys.platform != "darwin" or platform.machine() != "arm64":
            raise unittest.SkipTest("build-macos boundary test requires Darwin arm64")

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            scripts = fixture / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(ROOT / "scripts" / "build-macos.sh", scripts / "build-macos.sh")
            shutil.copy2(ATOMIC_PUBLISH, scripts / ATOMIC_PUBLISH.name)
            write_file(scripts / "verify-release-support.py", "raise SystemExit(99)\n")
            write_file(scripts / "scan-secrets.sh", "#!/usr/bin/env bash\nexit 99\n")
            (scripts / "scan-secrets.sh").chmod(0o755)
            fake_bin = fixture / "fake-bin"
            fake_bin.mkdir()
            write_file(
                fake_bin / "node",
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "if [[ \"$*\" == *tauri.js* ]]; then touch \"$FRIEND_BUILD_MARKER\"; fi\n"
                "printf 'v22.0.0\\n'\n",
            )
            (fake_bin / "node").chmod(0o755)

            temp_root = fixture / "temp-root"
            release_dir = temp_root / "release" / "macos"
            write_file(release_dir / "sentinel.txt", "existing release\n")
            marker = fixture / "build-called"
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]
            environment["FRIEND_RELEASE_DIR"] = str(release_dir)
            environment["FRIEND_RELEASE_TEMP_ROOT"] = str(temp_root)
            environment["FRIEND_BUILD_MARKER"] = str(marker)

            result = run_command(
                ["bash", str(scripts / "build-macos.sh")],
                cwd=fixture,
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("目标已存在", result.stderr)
            self.assertEqual(
                (release_dir / "sentinel.txt").read_text(encoding="utf-8"),
                "existing release\n",
            )
            self.assertFalse(marker.exists())

    def test_scanner_passes_clean_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._git_repository(Path(temporary))
            result = run_scan(repository)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("release scan: PASS:", result.stdout)

    def test_scanner_rejects_source_secret_without_echoing_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._git_repository(Path(temporary))
            secret = "sk-" + ("A" * 24)
            write_file(repository / "src" / "main.js", "const value = " + repr(secret) + ";\n")
            result = run_scan(repository)
            self.assert_scanner_blocked(
                result,
                "sensitive value or forbidden runtime marker in repository source boundary; contents withheld",
            )
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_scanner_covers_ignored_and_document_source_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._git_repository(Path(temporary))
            write_file(repository / ".gitignore", ".env\nnew-api-deployment/ignored.env\n")
            self._git(repository, "add", ".gitignore")
            self._git(repository, "commit", "-m", "fixture ignore rules")

            secret = "sk-" + ("C" * 24)
            paths = (
                "new-api-deployment/README.md",
                "new-api-deployment/ignored.env",
                "payment-control-plane/control_plane.py",
                "README.md",
                "IMPLEMENTATION_PLAN.md",
                ".env",
            )
            for relative_path in paths:
                path = repository / relative_path
                try:
                    if relative_path == ".env":
                        content = "MYSQL_PASSWORD=" + ("C" * 24) + "\n"
                    else:
                        content = "value = " + repr(secret) + "\n"
                    write_file(path, content)
                    result = run_scan(repository)
                    self.assert_scanner_blocked(
                        result,
                        "sensitive value or forbidden runtime marker in repository source boundary; contents withheld",
                    )
                    self.assertNotIn(secret, result.stdout + result.stderr, relative_path)
                finally:
                    path.unlink(missing_ok=True)

    def test_scanner_covers_app_bundle_and_external_mounted_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            repository = self._git_repository(temporary_root / "repository")
            secret = "sk-" + ("D" * 24)
            bundle_resource = (
                repository
                / "src-tauri"
                / "target"
                / "release"
                / "bundle"
                / "macos"
                / "Friend Claude.app"
                / "Contents"
                / "Resources"
                / "config.json"
            )
            write_file(bundle_resource, "value = " + repr(secret) + "\n")
            result = run_scan(repository)
            self.assert_scanner_blocked(
                result,
                "sensitive value or forbidden runtime marker in unpacked/artifact paths; contents withheld",
            )
            self.assertNotIn(secret, result.stdout + result.stderr)

            shutil.rmtree(repository / "src-tauri")
            mounted_bundle = (
                temporary_root
                / "mounted-dmg"
                / "Friend Claude.app"
                / "Contents"
                / "Resources"
                / "config.json"
            )
            write_file(mounted_bundle, "value = " + repr(secret) + "\n")
            result = run_scan(repository, temporary_root / "mounted-dmg")
            self.assert_scanner_blocked(
                result,
                "sensitive value or forbidden runtime marker in unpacked/artifact paths; contents withheld",
            )
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_scanner_rejects_git_history_after_worktree_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._git_repository(Path(temporary))
            secret = "sk-" + ("B" * 24)
            source = repository / "src" / "history.js"
            write_file(source, "const value = " + repr(secret) + ";\n")
            self._git(repository, "add", ".")
            self._git(repository, "commit", "-m", "temporary fixture")
            write_file(source, "const value = 'clean';\n")
            result = run_scan(repository)
            self.assert_scanner_blocked(
                result,
                "sensitive value or forbidden marker in Git history; contents withheld",
            )
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_scanner_rejects_artifact_marker_and_complete_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._git_repository(Path(temporary))
            marker = "local_" + "flow_id"
            write_file(repository / "dist" / "unpacked" / "config.json", json.dumps({"marker": marker}))
            result = run_scan(repository)
            self.assert_scanner_blocked(
                result,
                "sensitive value or forbidden runtime marker in unpacked/artifact paths; contents withheld",
            )

            shutil.rmtree(repository / "dist")
            write_file(repository / "release" / "request.log", "redacted fixture")
            result = run_scan(repository)
            self.assert_scanner_blocked(
                result,
                "complete-log file found in unpacked or artifact paths",
            )

    def assert_scanner_blocked(
        self, result: subprocess.CompletedProcess, message: str
    ) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"release scan: BLOCKED: {message}", result.stderr)

    @staticmethod
    def _require_friend_kit_tools() -> None:
        if os.name == "nt":
            raise unittest.SkipTest("friend kit fixture uses macOS shell tools")
        missing = [
            name
            for name in ("python3", "rg", "shasum", "unzip")
            if shutil.which(name) is None
        ]
        if missing:
            raise unittest.SkipTest("missing friend kit fixture tools: " + ", ".join(missing))

    def _create_prepare_fixture(
        self, fixture: Path, *, extra_release_file: bool = False
    ) -> Path:
        scripts = fixture / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(PREPARE, scripts / PREPARE.name)
        shutil.copy2(ATOMIC_PUBLISH, scripts / ATOMIC_PUBLISH.name)
        shutil.copy2(VERIFY, scripts / VERIFY.name)
        shutil.copy2(SCAN, scripts / SCAN.name)
        write_file(
            scripts / GATEWAY_VERIFY.name,
            "import argparse\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('--url', required=True)\n"
            "parser.parse_args()\n"
            "if __import__('os').environ.get('FRIEND_PREFLIGHT_MARKER'):\n"
            "    from pathlib import Path\n"
            "    Path(__import__('os').environ['FRIEND_PREFLIGHT_MARKER']).write_text('called\\n')\n"
            "if __import__('os').environ.get('FRIEND_PREFLIGHT_FAIL'):\n"
            "    raise SystemExit(1)\n"
            "print('friend gateway preflight: PASS: fixture')\n",
        )

        build_script = scripts / "build-macos.sh"
        build_script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "root_dir=$(cd \"$(dirname \"$0\")/..\" && pwd)\n"
            "release_dir=\"${FRIEND_RELEASE_DIR:-$root_dir/release/macos}\"\n"
            "mkdir -p \"$release_dir\"\n"
            "if [[ -n \"${FRIEND_BUILD_MARKER-}\" ]]; then printf 'built\\n' >\"$FRIEND_BUILD_MARKER\"; fi\n"
            "dmg=\"$release_dir/Friend-Claude-0.1.0-macos-arm64-candidate.dmg\"\n"
            "printf 'fixture dmg\\n' >\"$dmg\"\n"
            "printf '%s  %s\\n' \"$(shasum -a 256 \"$dmg\" | awk '{ print $1 }')\" \"$(basename \"$dmg\")\" >\"$release_dir/SHA256SUMS-candidate.txt\"\n",
            encoding="utf-8",
        )
        if extra_release_file:
            with build_script.open("a", encoding="utf-8") as handle:
                handle.write(
                    "printf 'unexpected fixture\\n' >\"$release_dir/unexpected.txt\"\n"
                )
        build_script.chmod(0o755)

        write_file(fixture / "src" / "main.js", "const value = 'clean';\n")
        run_command(["git", "init", "-q"], cwd=fixture)
        run_command(
            ["git", "config", "user.email", "release-gate@example.invalid"],
            cwd=fixture,
        )
        run_command(
            ["git", "config", "user.name", "release-gate-test"], cwd=fixture
        )
        run_command(["git", "add", "."], cwd=fixture)
        run_command(["git", "commit", "-qm", "fixture baseline"], cwd=fixture)

        temp_dir = fixture / "tmp"
        temp_dir.mkdir()
        return temp_dir

    @staticmethod
    def _git_repository(path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        run_command(["git", "init", "-q"], cwd=path)
        run_command(["git", "config", "user.email", "release-gate@example.invalid"], cwd=path)
        run_command(["git", "config", "user.name", "release-gate-test"], cwd=path)
        write_file(path / "src" / "main.js", "const value = 'clean';\n")
        write_file(
            path / "new-api-deployment" / ".env.example",
            "MYSQL_PASSWORD=replace-with-runtime-secret\n",
        )
        run_command(["git", "add", "."], cwd=path)
        run_command(["git", "commit", "-qm", "fixture baseline"], cwd=path)
        return path

    @staticmethod
    def _git(repository: Path, *arguments: str) -> None:
        result = run_command(["git", *arguments], cwd=repository)
        if result.returncode != 0:
            raise AssertionError(result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
