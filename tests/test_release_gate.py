#!/usr/bin/env python3
"""Runnable tests for the V1A release gates."""

from __future__ import annotations

import json
import hashlib
import importlib.util
import ntpath
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional


ROOT = Path(__file__).resolve().parent.parent
VERIFY = ROOT / "scripts" / "verify-release-support.py"
SCAN = ROOT / "scripts" / "scan-secrets.sh"


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
        self.assertIn("npm exec -- tauri build", macos_script)
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
        self.assertIn("desktop:build:macos", workflow)
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
