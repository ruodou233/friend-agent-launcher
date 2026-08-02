#!/usr/bin/env python3
"""Fail-closed checks for the current product/system release matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import stat
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple


EXPECTED_TARGETS = {
    ("claude", "macos"),
    ("claude", "windows"),
    ("codex", "macos"),
    ("codex", "windows"),
}
CURRENT_CANDIDATE = ("claude", "macos")
VALID_STATUSES = {"blocked", "candidate", "go"}
VALID_CHANNELS = {"blocked", "local-ci-only", "friend-release"}
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
WRAPPER_ENV = "FRIEND_RELEASE_BUILD_WRAPPER"
WRAPPER_VALUE = "scripts/build-macos.sh"
CANDIDATE_DMG_NAME = "Friend Claude_0.1.0_aarch64.dmg"
RELEASE_CANDIDATE_DMG_NAME = "Friend-Claude-0.1.0-macos-arm64-candidate.dmg"
RELEASE_CHECKSUM_NAME = "SHA256SUMS-candidate.txt"
# Tauri may remove the macOS .app staging tree after it creates the DMG, while
# create-dmg keeps its support templates beside the final artifact.
CANDIDATE_ROOT_ENTRIES = frozenset({"dmg", "macos", "share"})
CANDIDATE_REQUIRED_ROOT_ENTRIES = frozenset({"dmg", "share"})
CANDIDATE_DMG_HELPER_NAMES = frozenset({"bundle_dmg.sh", "icon.icns"})
CANDIDATE_DMG_REQUIRED_HELPER_NAMES = frozenset({"bundle_dmg.sh"})
EXPECTED_SHARE_FILES = frozenset({"template.applescript", "eula-resources-template.xml"})
# Finder may create .DS_Store in a mounted DMG on some macOS environments and
# omit it in others. Keep that variability limited to this exact, non-runtime
# metadata name; all other root entries remain part of the strict allowlist.
MOUNTED_DMG_REQUIRED_ROOT_FILES = frozenset({".VolumeIcon.icns"})
MOUNTED_DMG_OPTIONAL_ROOT_FILES = frozenset({".DS_Store"})
MOUNTED_DMG_APP_CONTENTS = frozenset({"Info.plist", "MacOS", "Resources"})
REQUIRED_MACH_O_ARCHS = "arm64"


class ReleaseGateError(Exception):
    """A user-actionable release gate failure without sensitive details."""


def _target_label(product: str, system: str) -> str:
    return f"{product}/{system}"


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseGateError(f"{field} must be a non-empty string")
    return value


def _load_document(path: Path) -> Dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReleaseGateError(f"support file is missing: {path.name}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseGateError(f"support file is not valid JSON: {path.name}") from exc

    if not isinstance(document, dict):
        raise ReleaseGateError("support file root must be an object")
    return document


def _validate_policy(document: Dict[str, Any]) -> None:
    if document.get("schema_version") != 1:
        raise ReleaseGateError("unsupported support-file schema")

    policy = document.get("policy")
    if not isinstance(policy, dict):
        raise ReleaseGateError("policy must be an object")
    if policy.get("node_major") != 22:
        raise ReleaseGateError("Node policy must be major version 22")
    if policy.get("candidate_upload") is not False:
        raise ReleaseGateError("candidate_upload must remain false")
    required = policy.get("friend_upload_requires")
    if required != ["status=go", "p0_evidence_digest"]:
        raise ReleaseGateError("friend upload must require status=go and a P0 digest")


def _validate_targets(document: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    targets = document.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ReleaseGateError("targets must be a non-empty array")

    indexed: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for target in targets:
        if not isinstance(target, dict):
            raise ReleaseGateError("each target must be an object")
        product = _require_string(target.get("product"), "target.product")
        system = _require_string(target.get("system"), "target.system")
        key = (product, system)
        if key in indexed:
            raise ReleaseGateError(f"duplicate target: {_target_label(product, system)}")
        if key not in EXPECTED_TARGETS:
            raise ReleaseGateError(f"unexpected target: {_target_label(product, system)}")

        status = _require_string(target.get("status"), "target.status")
        channel = _require_string(target.get("channel"), "target.channel")
        digest = target.get("p0_evidence_digest")
        if status not in VALID_STATUSES:
            raise ReleaseGateError(f"invalid status for {_target_label(product, system)}")
        if channel not in VALID_CHANNELS:
            raise ReleaseGateError(f"invalid channel for {_target_label(product, system)}")

        if status == "blocked":
            if channel != "blocked" or digest is not None:
                raise ReleaseGateError(f"blocked target is not releasable: {_target_label(product, system)}")
        elif status == "candidate":
            if channel != "local-ci-only" or digest is not None:
                raise ReleaseGateError(f"candidate target must be local-only and undigested: {_target_label(product, system)}")
        else:
            if channel != "friend-release" or not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
                raise ReleaseGateError(f"go target needs a sha256 P0 digest: {_target_label(product, system)}")

        indexed[key] = target

    if set(indexed) != EXPECTED_TARGETS:
        missing = EXPECTED_TARGETS - set(indexed)
        labels = ", ".join(sorted(_target_label(*key) for key in missing))
        raise ReleaseGateError(f"support matrix is incomplete: {labels}")
    return indexed


def load_matrix(path: Path) -> Dict[Tuple[str, str], Dict[str, Any]]:
    document = _load_document(path)
    _validate_policy(document)
    return _validate_targets(document)


def validate_current_boundary(matrix: Dict[Tuple[str, str], Dict[str, Any]]) -> None:
    candidate_targets = [key for key, target in matrix.items() if target["status"] == "candidate"]
    releasable_targets = [key for key, target in matrix.items() if target["status"] == "go"]
    if candidate_targets != [CURRENT_CANDIDATE] or releasable_targets:
        raise ReleaseGateError("V1A permits only Claude macOS candidate; all other targets must be blocked")
    for key, target in matrix.items():
        if key != CURRENT_CANDIDATE and target["status"] != "blocked":
            raise ReleaseGateError(f"V1A target must be blocked: {_target_label(*key)}")


def _walk_without_symlinks(root: Path, label: str) -> Iterator[Path]:
    """Walk a release tree with lstat semantics and fail on unsafe entries."""

    try:
        root_info = os.lstat(root)
    except FileNotFoundError as exc:
        raise ReleaseGateError(f"{label} is missing: {root}") from exc
    except OSError as exc:
        raise ReleaseGateError(f"{label} cannot be inspected") from exc
    if stat.S_ISLNK(root_info.st_mode):
        raise ReleaseGateError(f"{label} must not be a symlink")
    if not stat.S_ISDIR(root_info.st_mode):
        raise ReleaseGateError(f"{label} must be a directory")

    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            directory_info = os.lstat(directory)
        except OSError as exc:
            raise ReleaseGateError(f"{label} cannot be inspected") from exc
        if stat.S_ISLNK(directory_info.st_mode):
            relative = directory.relative_to(root) if directory != root else Path(".")
            raise ReleaseGateError(f"{label} contains a symlink: {relative}")
        if not stat.S_ISDIR(directory_info.st_mode):
            raise ReleaseGateError(f"{label} contains a non-directory traversal node")

        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise ReleaseGateError(f"{label} cannot be inspected") from exc
        for entry in entries:
            child = Path(entry.path)
            try:
                child_info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ReleaseGateError(f"{label} cannot be inspected") from exc
            relative = child.relative_to(root)
            if stat.S_ISLNK(child_info.st_mode):
                raise ReleaseGateError(f"{label} contains a symlink: {relative}")
            if not stat.S_ISREG(child_info.st_mode) and not stat.S_ISDIR(child_info.st_mode):
                raise ReleaseGateError(f"{label} contains an unsupported entry: {relative}")
            yield child
            if stat.S_ISDIR(child_info.st_mode):
                pending.append(child)


def _is_real_file(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def _is_real_directory(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode)


def _validate_candidate_share(tree: Iterable[Path], share_dir: Path) -> None:
    create_dmg_dir = share_dir / "create-dmg"
    support_dir = create_dmg_dir / "support"

    share_entries = [path for path in tree if path.parent == share_dir]
    if len(share_entries) != 1 or share_entries[0].name != "create-dmg":
        raise ReleaseGateError("candidate share/ must contain only create-dmg/")
    if not _is_real_directory(share_entries[0]):
        raise ReleaseGateError("candidate share/create-dmg must be a directory")

    create_dmg_entries = [path for path in tree if path.parent == create_dmg_dir]
    if len(create_dmg_entries) != 1 or create_dmg_entries[0].name != "support":
        raise ReleaseGateError("candidate share/create-dmg/ must contain only support/")
    if not _is_real_directory(create_dmg_entries[0]):
        raise ReleaseGateError("candidate share/create-dmg/support must be a directory")

    support_entries = [path for path in tree if path.parent == support_dir]
    if {path.name for path in support_entries} != EXPECTED_SHARE_FILES:
        raise ReleaseGateError("candidate share support must contain only the two expected templates")
    if any(not _is_real_file(path) for path in support_entries):
        raise ReleaseGateError("candidate share support entries must be regular files")


def validate_candidate_bundle(bundle_dir: Path) -> Path:
    """Validate a fresh Tauri bundle and return the only DMG to copy.

    Tauri may remove the macOS app staging tree after creating the DMG, so
    macos/ is optional and must be empty while the DMG and create-dmg support
    files remain mandatory.
    """

    tree = list(_walk_without_symlinks(bundle_dir, "candidate bundle"))

    root_entries = [path for path in tree if path.parent == bundle_dir]
    root_entry_names = {path.name for path in root_entries}
    if (
        not CANDIDATE_REQUIRED_ROOT_ENTRIES <= root_entry_names
        or not root_entry_names <= CANDIDATE_ROOT_ENTRIES
    ):
        raise ReleaseGateError(
            "candidate bundle root must contain dmg/ and share/ and only optional macos/"
        )
    if any(not _is_real_directory(path) for path in root_entries):
        raise ReleaseGateError("candidate bundle root entries must be directories")

    dmg_dir = bundle_dir / "dmg"
    macos_dir = bundle_dir / "macos"
    share_dir = bundle_dir / "share"
    dmg_entries = [path for path in tree if path.parent == dmg_dir]
    dmg_entry_names = {path.name for path in dmg_entries}
    required_dmg_names = {CANDIDATE_DMG_NAME} | CANDIDATE_DMG_REQUIRED_HELPER_NAMES
    if (
        not required_dmg_names <= dmg_entry_names
        or not dmg_entry_names <= {CANDIDATE_DMG_NAME} | CANDIDATE_DMG_HELPER_NAMES
    ):
        raise ReleaseGateError(
            "candidate dmg/ must contain the final DMG, bundle_dmg.sh, and only optional icon.icns"
        )
    if any(not _is_real_file(path) for path in dmg_entries):
        raise ReleaseGateError("candidate dmg/ entries must be regular files")

    _validate_candidate_share(tree, share_dir)

    if macos_dir in root_entries:
        macos_entries = [path for path in tree if path.parent == macos_dir]
        if macos_entries:
            raise ReleaseGateError("candidate macos/ must be empty")

    final_dmg = dmg_dir / CANDIDATE_DMG_NAME
    if not _is_real_file(final_dmg):
        raise ReleaseGateError("candidate final DMG must remain a regular file")
    return final_dmg


def validate_release_directory(release_dir: Path, require_outputs: bool = False) -> None:
    """Keep release/macos limited to the candidate DMG and its checksum."""

    tree = list(_walk_without_symlinks(release_dir, "release directory"))

    allowed_names = {RELEASE_CANDIDATE_DMG_NAME, RELEASE_CHECKSUM_NAME}
    root_entries = [path for path in tree if path.parent == release_dir]
    unexpected = sorted(path.name for path in root_entries if path.name not in allowed_names)
    if unexpected:
        raise ReleaseGateError("release directory contains unexpected entries: " + ", ".join(unexpected))
    if any(path.parent != release_dir for path in tree):
        raise ReleaseGateError("release directory must contain only the candidate DMG and checksum files")

    expected_paths = [release_dir / name for name in sorted(allowed_names)]
    for path in expected_paths:
        if path in root_entries and not _is_real_file(path):
            raise ReleaseGateError(f"release entry must be a file: {path.name}")
    if require_outputs and any(path not in root_entries or not _is_real_file(path) for path in expected_paths):
        raise ReleaseGateError("release directory must contain the candidate DMG and checksum")


def _require_exact_entries(
    directory: Path,
    expected: Iterable[str],
    label: str,
    *,
    optional: Iterable[str] = (),
) -> Dict[str, Path]:
    """Return direct children from an exact allowlist, with an optional subset."""

    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        raise ReleaseGateError(f"{label} cannot be inspected") from exc
    expected_names = set(expected)
    optional_names = set(optional)
    if not optional_names <= expected_names:
        raise ReleaseGateError(f"{label} has an invalid optional-entry configuration")
    required_names = expected_names - optional_names
    actual_names = {entry.name for entry in entries}
    missing = sorted(required_names - actual_names)
    unexpected = sorted(actual_names - expected_names)
    if missing or unexpected:
        details = ", ".join(unexpected or missing)
        raise ReleaseGateError(f"{label} has an unexpected or missing entry: {details}")
    return {entry.name: Path(entry.path) for entry in entries}


def _require_real_file(path: Path, label: str) -> None:
    if not _is_real_file(path):
        raise ReleaseGateError(f"{label} must be a regular file")


def _require_real_directory(path: Path, label: str) -> None:
    if not _is_real_directory(path):
        raise ReleaseGateError(f"{label} must be a directory")


def validate_mounted_dmg(mount_dir: Path) -> None:
    """Validate the exact unsigned Claude candidate tree exposed by hdiutil."""

    try:
        root_info = os.lstat(mount_dir)
    except (FileNotFoundError, OSError) as exc:
        raise ReleaseGateError("mounted DMG is missing or cannot be inspected") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ReleaseGateError("mounted DMG root must be a real directory")

    root_entries = _require_exact_entries(
        mount_dir,
        MOUNTED_DMG_REQUIRED_ROOT_FILES
        | MOUNTED_DMG_OPTIONAL_ROOT_FILES
        | {"Applications", "Friend Claude.app"},
        "mounted DMG root",
        optional=MOUNTED_DMG_OPTIONAL_ROOT_FILES,
    )
    # .DS_Store is opaque Finder metadata, not a runtime payload. Its format
    # and filesystem metadata vary across macOS versions, so lstat must prove
    # only that an explicitly allowlisted instance is a regular file; the
    # caller's artifact scan still inspects its bytes for forbidden content.
    for name in MOUNTED_DMG_REQUIRED_ROOT_FILES | MOUNTED_DMG_OPTIONAL_ROOT_FILES:
        if name in root_entries:
            _require_real_file(root_entries[name], f"mounted DMG {name}")

    applications = root_entries["Applications"]
    try:
        applications_info = os.lstat(applications)
        applications_target = os.readlink(applications) if stat.S_ISLNK(applications_info.st_mode) else None
    except OSError as exc:
        raise ReleaseGateError("mounted DMG Applications link cannot be inspected") from exc
    if not stat.S_ISLNK(applications_info.st_mode) or applications_target != "/Applications":
        raise ReleaseGateError("mounted DMG Applications must link exactly to /Applications")

    app = root_entries["Friend Claude.app"]
    _require_real_directory(app, "mounted DMG Friend Claude.app")
    contents = app / "Contents"
    _require_real_directory(contents, "Friend Claude.app/Contents")
    contents_entries = _require_exact_entries(contents, MOUNTED_DMG_APP_CONTENTS, "Friend Claude.app/Contents")
    _require_real_file(contents_entries["Info.plist"], "Friend Claude.app/Contents/Info.plist")

    macos_dir = contents_entries["MacOS"]
    _require_real_directory(macos_dir, "Friend Claude.app/Contents/MacOS")
    macos_entries = _require_exact_entries(macos_dir, {"friend-agent-launcher"}, "Friend Claude.app/Contents/MacOS")
    _require_real_file(macos_entries["friend-agent-launcher"], "Friend Claude.app/Contents/MacOS/friend-agent-launcher")

    resources_dir = contents_entries["Resources"]
    _require_real_directory(resources_dir, "Friend Claude.app/Contents/Resources")
    resources_entries = _require_exact_entries(resources_dir, {"icon.icns"}, "Friend Claude.app/Contents/Resources")
    _require_real_file(resources_entries["icon.icns"], "Friend Claude.app/Contents/Resources/icon.icns")


def validate_macho_archs(archs: str) -> None:
    """Require the exact text emitted by ``lipo -archs`` for this candidate."""

    if archs != REQUIRED_MACH_O_ARCHS:
        raise ReleaseGateError("Friend Claude.app launcher must be arm64-only")


def _require_wrapper() -> None:
    """Apply an accidental-misuse guard, not an unforgeable build boundary."""

    if os.environ.get(WRAPPER_ENV) != WRAPPER_VALUE:
        raise ReleaseGateError("candidate build wrapper guard failed; use scripts/build-macos.sh")


def _file_digest(path: Path) -> str:
    if not path.is_file():
        raise ReleaseGateError(f"P0 evidence file is missing: {path}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReleaseGateError("P0 evidence file cannot be read") from exc
    return "sha256:" + digest.hexdigest()


def validate_evidence_binding(target: Dict[str, Any], evidence_file: Optional[Path]) -> None:
    """Bind a future go target to the exact bytes of a redacted evidence file."""

    if evidence_file is None:
        raise ReleaseGateError("friend upload requires --p0-evidence-file")
    actual_digest = _file_digest(evidence_file)
    if actual_digest != target.get("p0_evidence_digest"):
        raise ReleaseGateError("P0 evidence digest does not match the support matrix")


def host_matches(expected: str) -> bool:
    actual = platform.system().lower()
    if expected == "macos":
        return actual == "darwin"
    if expected == "windows":
        return actual == "windows"
    return actual == expected


def run_gate(
    path: Path,
    product: Optional[str],
    system: Optional[str],
    action: str,
    required_host: Optional[str],
    require_wrapper: bool,
    p0_evidence_file: Optional[Path],
) -> int:
    matrix = load_matrix(path)
    if required_host and not host_matches(required_host):
        raise ReleaseGateError(f"host does not match required system: {required_host}")

    if action == "upload":
        # This must remain first: the current V1A matrix has no friend-release target.
        validate_current_boundary(matrix)

    if action == "check":
        validate_current_boundary(matrix)
        print("release gate: V1A matrix is Claude macOS candidate-only")
        return 0

    if not product or not system:
        raise ReleaseGateError(f"--product and --system are required for action={action}")
    key = (product, system)
    if key not in matrix:
        raise ReleaseGateError(f"target is not registered: {_target_label(product, system)}")
    target = matrix[key]
    status = target["status"]

    if action == "build":
        validate_current_boundary(matrix)
        if require_wrapper:
            _require_wrapper()
        if key != CURRENT_CANDIDATE or status != "candidate":
            raise ReleaseGateError(f"candidate build is blocked: {_target_label(product, system)}")
        print(f"release gate: local candidate build allowed for {_target_label(product, system)}")
        return 0

    if action == "upload":
        if status != "go" or not isinstance(target.get("p0_evidence_digest"), str):
            raise ReleaseGateError(f"friend upload blocked until status=go with P0 digest: {_target_label(product, system)}")
        validate_evidence_binding(target, p0_evidence_file)
        print(f"release gate: friend upload precondition satisfied for {_target_label(product, system)}")
        return 0

    raise ReleaseGateError(f"unsupported action: {action}")


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    default_file = Path(__file__).resolve().parent.parent / "release-support.json"
    parser.add_argument("--file", type=Path, default=default_file)
    parser.add_argument("--product", choices=("claude", "codex"))
    parser.add_argument("--system", choices=("macos", "windows"))
    parser.add_argument(
        "--action",
        choices=("check", "build", "upload", "artifact-check", "macho-archs"),
        default="check",
    )
    parser.add_argument("--require-host", choices=("macos", "windows"))
    parser.add_argument("--require-wrapper", action="store_true")
    parser.add_argument("--p0-evidence-file", type=Path)
    parser.add_argument("--candidate-bundle", type=Path)
    parser.add_argument("--release-dir", type=Path)
    parser.add_argument("--dmg-mount", type=Path)
    parser.add_argument("--mach-o-archs")
    parser.add_argument("--require-release-files", action="store_true")
    return parser.parse_args(list(argv))


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.action == "artifact-check":
            if args.candidate_bundle is None and args.release_dir is None and args.dmg_mount is None:
                raise ReleaseGateError(
                    "artifact-check requires --candidate-bundle, --release-dir, or --dmg-mount"
                )
            if args.candidate_bundle is not None:
                validate_candidate_bundle(args.candidate_bundle)
                print("release gate: fresh Claude bundle allowlist passed")
            if args.release_dir is not None:
                validate_release_directory(args.release_dir, require_outputs=args.require_release_files)
                print("release gate: release/macos allowlist passed")
            if args.dmg_mount is not None:
                validate_mounted_dmg(args.dmg_mount)
                print("release gate: mounted DMG allowlist passed")
            return 0
        if args.action == "macho-archs":
            if args.mach_o_archs is None:
                raise ReleaseGateError("macho-archs requires --mach-o-archs")
            validate_macho_archs(args.mach_o_archs)
            print("release gate: Mach-O architecture allowlist passed")
            return 0
        return run_gate(
            args.file,
            args.product,
            args.system,
            args.action,
            args.require_host,
            args.require_wrapper,
            args.p0_evidence_file,
        )
    except ReleaseGateError as exc:
        print(f"release gate: BLOCKED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
