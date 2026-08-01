use fs2::FileExt;
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{self, ErrorKind, Write},
    path::{Path, PathBuf},
    sync::{Mutex, MutexGuard, OnceLock},
};

pub(crate) const FRIEND_OWNER: &str = "friend-agent";
pub(crate) const FRIEND_PRODUCT: &str = "claude";
pub(crate) const MANIFEST_SCHEMA_VERSION: u8 = 1;
pub(crate) const RECOVERY_JOURNAL_SCHEMA_VERSION: u8 = 1;
const CLAUDE_TRANSACTION_LOCK_FILE: &str = ".friend-agent.lock";

static CLAUDE_TRANSACTION_LOCK: OnceLock<Mutex<()>> = OnceLock::new();

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub(crate) enum Phase {
    Preflight,
    OwnershipCapture,
    WriteNewGeneration,
    ReadbackVerify,
    MetadataSwitch,
    OfficialAppVerify,
    Commit,
    DeleteOldFriendGeneration,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub(crate) enum CommitState {
    Pending,
    Committed,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub(crate) enum DeleteState {
    NotStarted,
    Deleted,
    RecoveryRequired,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct GenerationManifest {
    pub(crate) schema_version: u8,
    pub(crate) generation_id: String,
    /// The short-lived parent used only while deleting the previous Friend generation.
    pub(crate) parent_generation_id: Option<String>,
    /// The stable official/user applied ID captured before the first Friend takeover.
    pub(crate) previous_applied_id: Option<String>,
    pub(crate) profile_path: String,
    pub(crate) manifest_path: String,
    pub(crate) metadata_path: String,
    pub(crate) owner: String,
    pub(crate) product: String,
    pub(crate) install_binding: String,
    pub(crate) field_set: Vec<String>,
    pub(crate) profile_before_sha256: Option<String>,
    pub(crate) profile_after_sha256: Option<String>,
    pub(crate) metadata_before_sha256: Option<String>,
    pub(crate) metadata_after_sha256: Option<String>,
    pub(crate) expected_catalog_version: String,
    pub(crate) phase: Phase,
    pub(crate) commit_state: CommitState,
    pub(crate) delete_state: DeleteState,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub(crate) struct RecoveryJournal {
    pub(crate) schema_version: u8,
    pub(crate) status: String,
    pub(crate) generation_id: String,
    pub(crate) phase: Phase,
    pub(crate) metadata_before_sha256: Option<String>,
    pub(crate) metadata_after_sha256: Option<String>,
    pub(crate) reason: String,
}

#[derive(Debug, Clone)]
pub(crate) struct FileSnapshot {
    pub(crate) exists: bool,
    pub(crate) bytes: Vec<u8>,
}

impl FileSnapshot {
    pub(crate) fn sha256(&self) -> Option<String> {
        self.exists.then(|| sha256_hex(&self.bytes))
    }
}

pub(crate) fn recovery_required(reason: &str) -> String {
    format!("RECOVERY_REQUIRED: {reason}")
}

pub(crate) struct ClaudeTransactionGuard {
    _process_guard: MutexGuard<'static, ()>,
    _cooperative_lock: fs::File,
}

pub(crate) fn claude_transaction_lock_path(library: &Path) -> PathBuf {
    library.join(CLAUDE_TRANSACTION_LOCK_FILE)
}

/// The OS lock coordinates only processes that use this lock file. It is not a
/// kernel-enforced CAS and cannot stop an unrelated writer from racing us.
pub(crate) fn lock_claude_transaction(library: &Path) -> Result<ClaudeTransactionGuard, String> {
    let process_guard = CLAUDE_TRANSACTION_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .map_err(|_| recovery_required("Claude 进程事务锁已失效"))?;

    fs::create_dir_all(library).map_err(|_| recovery_required("Claude 配置库无法创建或访问"))?;
    let lock_path = claude_transaction_lock_path(library);
    let mut options = fs::OpenOptions::new();
    options.create(true).read(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let cooperative_lock = options
        .open(&lock_path)
        .map_err(|_| recovery_required("Claude 文件事务协作锁无法打开"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&lock_path, fs::Permissions::from_mode(0o600))
            .map_err(|_| recovery_required("Claude 文件事务协作锁权限无法设置"))?;
    }
    cooperative_lock
        .lock_exclusive()
        .map_err(|_| recovery_required("Claude 文件事务协作锁无法取得"))?;

    Ok(ClaudeTransactionGuard {
        _process_guard: process_guard,
        _cooperative_lock: cooperative_lock,
    })
}

pub(crate) fn snapshot(path: &Path, label: &str) -> Result<FileSnapshot, String> {
    match fs::read(path) {
        Ok(bytes) => Ok(FileSnapshot {
            exists: true,
            bytes,
        }),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(FileSnapshot {
            exists: false,
            bytes: Vec::new(),
        }),
        Err(_) => Err(format!("读取{label}失败")),
    }
}

pub(crate) fn current_matches(path: &Path, expected: &FileSnapshot) -> Result<bool, String> {
    let current = snapshot(path, "当前文件")?;
    Ok(snapshots_match(&current, expected))
}

fn snapshots_match(left: &FileSnapshot, right: &FileSnapshot) -> bool {
    left.exists == right.exists && left.bytes == right.bytes
}

pub(crate) fn write_json_atomic<T: Serialize>(path: &Path, value: &T) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(value).map_err(|_| "编码配置失败".to_string())?;
    write_bytes_atomic(path, &bytes)
}

pub(crate) fn read_json<T: DeserializeOwned>(
    path: &Path,
    label: &str,
) -> Result<Option<T>, String> {
    let data = match fs::read(path) {
        Ok(data) => data,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(format!("读取{label}失败")),
    };
    serde_json::from_slice(&data)
        .map(Some)
        .map_err(|_| format!("{label}格式无效"))
}

pub(crate) fn write_bytes_atomic(path: &Path, data: &[u8]) -> Result<(), String> {
    let parent = path.parent().ok_or_else(|| "目标目录无效".to_string())?;
    fs::create_dir_all(parent).map_err(|_| "创建配置目录失败".to_string())?;
    let temporary = path.with_extension("friend-agent.tmp");
    let backup = path.with_extension("friend-agent.bak");
    let _ = fs::remove_file(&temporary);

    let mut options = fs::OpenOptions::new();
    options.create_new(true).read(true).write(true);
    let mut temporary_file = match options.open(&temporary) {
        Ok(file) => file,
        Err(_) => {
            let _ = fs::remove_file(&temporary);
            return Err("写入临时配置失败".into());
        }
    };

    let write_result = (|| -> Result<(), String> {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = fs::metadata(path)
                .map(|metadata| metadata.permissions().mode())
                .unwrap_or(0o600);
            fs::set_permissions(&temporary, fs::Permissions::from_mode(mode))
                .map_err(|_| "设置配置权限失败".to_string())?;
        }

        temporary_file
            .write_all(data)
            .map_err(|_| "写入临时配置失败".to_string())?;
        temporary_file
            .flush()
            .map_err(|_| "刷新临时配置失败".to_string())?;
        temporary_file
            .sync_all()
            .map_err(|_| "同步临时配置失败".to_string())
    })();
    drop(temporary_file);
    if let Err(error) = write_result {
        let _ = fs::remove_file(&temporary);
        return Err(error);
    }

    if backup.exists() {
        if path.exists() {
            if fs::remove_file(&backup).is_err() {
                let _ = fs::remove_file(&temporary);
                return Err("清理旧配置备份失败".into());
            }
        } else {
            if fs::rename(&backup, path).is_err() {
                let _ = fs::remove_file(&temporary);
                return Err("恢复中断配置失败".into());
            }
        }
    }
    if path.exists() {
        if fs::rename(path, &backup).is_err() {
            let _ = fs::remove_file(&temporary);
            return Err("暂存现有配置失败".into());
        }
    }
    if fs::rename(&temporary, path).is_err() {
        if backup.exists() {
            let _ = fs::rename(&backup, path);
        }
        let _ = fs::remove_file(&temporary);
        return Err("提交配置失败".into());
    }
    let _ = fs::remove_file(&backup);
    Ok(())
}

/// Write a sensitive Friend profile directly to its final generation path.
/// The path is create-new only: no temporary or backup path may contain the
/// profile bytes. A partial final file is removed on a write/flush/sync error.
pub(crate) fn write_sensitive_profile<T: Serialize>(path: &Path, value: &T) -> Result<(), String> {
    let parent = path.parent().ok_or_else(|| "目标目录无效".to_string())?;
    fs::create_dir_all(parent).map_err(|_| "创建 Friend profile 目录失败".to_string())?;
    let bytes =
        serde_json::to_vec_pretty(value).map_err(|_| "编码 Friend profile 失败".to_string())?;

    let mut options = fs::OpenOptions::new();
    options.create_new(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options
        .open(path)
        .map_err(|_| "Friend profile 最终 generation path 无法创建".to_string())?;

    let write_result = (|| -> Result<(), String> {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            file.set_permissions(fs::Permissions::from_mode(0o600))
                .map_err(|_| "Friend profile 权限无法设置".to_string())?;
        }
        file.write_all(&bytes)
            .map_err(|_| "Friend profile 写入失败".to_string())?;
        file.flush()
            .map_err(|_| "Friend profile flush 失败".to_string())?;
        file.sync_all()
            .map_err(|_| "Friend profile sync 失败".to_string())
    })();

    if let Err(error) = write_result {
        drop(file);
        return match cleanup_failed_sensitive_profile(path) {
            Ok(()) => Err(error),
            Err(cleanup_error) => Err(cleanup_error),
        };
    }
    Ok(())
}

fn cleanup_failed_sensitive_profile(path: &Path) -> Result<(), String> {
    cleanup_failed_sensitive_profile_with(path, |path| fs::remove_file(path))
}

fn cleanup_failed_sensitive_profile_with<F>(path: &Path, remove_file: F) -> Result<(), String>
where
    F: FnOnce(&Path) -> io::Result<()>,
{
    let _ = remove_file(path);
    match snapshot(path, "失败后的 Friend profile") {
        Ok(FileSnapshot { exists: false, .. }) => Ok(()),
        Ok(FileSnapshot { exists: true, .. }) => Err(recovery_required(
            "Friend profile 写入失败且残留文件无法清理",
        )),
        Err(_) => Err(recovery_required(
            "Friend profile 写入失败且残留状态无法确认",
        )),
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ManifestWriteError {
    Conflict(String),
    Write(String),
}

impl ManifestWriteError {
    pub(crate) fn into_string(self) -> String {
        match self {
            Self::Conflict(error) | Self::Write(error) => error,
        }
    }
}

fn write_bytes_if_unchanged_classified(
    path: &Path,
    expected: &FileSnapshot,
    data: &[u8],
) -> Result<FileSnapshot, ManifestWriteError> {
    let current = snapshot(path, "写入前文件").map_err(ManifestWriteError::Write)?;
    if !snapshots_match(&current, expected) {
        return Err(ManifestWriteError::Conflict(recovery_required(
            "文件在写入前被外部修改",
        )));
    }
    let final_check = snapshot(path, "最终写入动作前文件").map_err(ManifestWriteError::Write)?;
    if !snapshots_match(&final_check, expected) {
        return Err(ManifestWriteError::Conflict(recovery_required(
            "文件在最终写入动作前被外部修改",
        )));
    }
    write_bytes_atomic(path, data).map_err(ManifestWriteError::Write)?;
    snapshot(path, "写入后文件").map_err(ManifestWriteError::Write)
}

/// This is a best-effort unchanged-snapshot check. The process and OS locks
/// protect cooperating writers, but the final check and write are not a
/// kernel CAS against unrelated processes.
pub(crate) fn write_bytes_if_unchanged(
    path: &Path,
    expected: &FileSnapshot,
    data: &[u8],
) -> Result<FileSnapshot, String> {
    write_bytes_if_unchanged_classified(path, expected, data)
        .map_err(ManifestWriteError::into_string)
}

pub(crate) fn write_json_if_unchanged<T: Serialize>(
    path: &Path,
    expected: &FileSnapshot,
    value: &T,
) -> Result<FileSnapshot, String> {
    let bytes = serde_json::to_vec_pretty(value).map_err(|_| "编码配置失败".to_string())?;
    write_bytes_if_unchanged(path, expected, &bytes)
}

pub(crate) fn restore_snapshot_if_unchanged(
    path: &Path,
    expected_current: &FileSnapshot,
    target: &FileSnapshot,
) -> Result<FileSnapshot, String> {
    let current = snapshot(path, "恢复前文件")?;
    if !snapshots_match(&current, expected_current) {
        return Err(recovery_required("恢复前文件已被外部修改"));
    }
    if target.exists {
        write_bytes_if_unchanged(path, expected_current, &target.bytes)
    } else {
        remove_file_if_unchanged(path, expected_current, "恢复目标文件")?;
        snapshot(path, "恢复后文件")
    }
}

pub(crate) fn remove_file_if_unchanged(
    path: &Path,
    expected: &FileSnapshot,
    label: &str,
) -> Result<(), String> {
    let current = snapshot(path, label)?;
    if !snapshots_match(&current, expected) {
        return Err(recovery_required(&format!("{label}在删除前被外部修改")));
    }
    if !expected.exists {
        return Ok(());
    }
    let final_check = snapshot(path, label)?;
    if !snapshots_match(&final_check, expected) {
        return Err(recovery_required(&format!(
            "{label}在最终删除动作前被外部修改"
        )));
    }
    fs::remove_file(path).map_err(|_| recovery_required(&format!("{label}删除结果不确定")))?;
    if snapshot(path, label)?.exists {
        return Err(recovery_required(&format!("{label}删除结果不确定")));
    }
    Ok(())
}

pub(crate) fn recovery_journal_path(directory: &Path) -> std::path::PathBuf {
    directory.join("RECOVERY_REQUIRED.json")
}

fn sync_file_and_parent(path: &Path) -> Result<(), String> {
    let file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .map_err(|_| recovery_required("RECOVERY_REQUIRED journal 读回后无法同步"))?;
    file.sync_all()
        .map_err(|_| recovery_required("RECOVERY_REQUIRED journal 无法同步"))?;

    // Unix can fsync the parent directory to persist the atomic rename. Windows
    // has no portable directory-fsync operation in std; the successful file
    // flush/sync above is the available content-durability guarantee there, so
    // the missing directory sync is not treated as a failed journal write.
    #[cfg(unix)]
    if let Some(parent) = path.parent() {
        let directory = fs::File::open(parent)
            .map_err(|_| recovery_required("RECOVERY_REQUIRED journal 目录无法同步"))?;
        directory
            .sync_all()
            .map_err(|_| recovery_required("RECOVERY_REQUIRED journal 目录无法同步"))?;
    }

    Ok(())
}

pub(crate) fn ensure_no_recovery_journal(
    library: &Path,
    manifest_dir: &Path,
) -> Result<(), String> {
    let paths: [PathBuf; 2] = [
        recovery_journal_path(library),
        recovery_journal_path(manifest_dir),
    ];
    let mut found = false;
    let mut check_failed = false;

    for path in paths {
        match fs::symlink_metadata(path) {
            Ok(_) => found = true,
            Err(error) if error.kind() == ErrorKind::NotFound => {}
            Err(_) => check_failed = true,
        }
    }

    if found {
        Err(recovery_required("存在待处理 RECOVERY_REQUIRED journal"))
    } else if check_failed {
        Err(recovery_required("无法检查 RECOVERY_REQUIRED journal"))
    } else {
        Ok(())
    }
}

#[cfg(test)]
pub(crate) fn recovery_journal_exists(directory: &Path) -> Result<bool, String> {
    match fs::symlink_metadata(recovery_journal_path(directory)) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(false),
        Err(_) => Err("读取 RECOVERY_REQUIRED journal 失败".into()),
    }
}

pub(crate) fn write_recovery_journal(
    path: &Path,
    generation_id: &str,
    phase: &Phase,
    metadata_before_sha256: Option<String>,
    metadata_after_sha256: Option<String>,
    reason: &str,
) -> Result<(), String> {
    let journal = RecoveryJournal {
        schema_version: RECOVERY_JOURNAL_SCHEMA_VERSION,
        status: "RECOVERY_REQUIRED".into(),
        generation_id: generation_id.into(),
        phase: phase.clone(),
        metadata_before_sha256,
        metadata_after_sha256,
        reason: reason.into(),
    };
    let bytes = serde_json::to_vec_pretty(&journal)
        .map_err(|_| recovery_required("RECOVERY_REQUIRED journal 编码失败"))?;
    write_bytes_atomic(path, &bytes)
        .map_err(|_| recovery_required("RECOVERY_REQUIRED journal 写入失败"))?;

    let read_back: RecoveryJournal = read_json(path, "RECOVERY_REQUIRED journal")?
        .ok_or_else(|| recovery_required("RECOVERY_REQUIRED journal 读回失败"))?;
    if read_back != journal {
        return Err(recovery_required(
            "RECOVERY_REQUIRED journal 读回内容不匹配",
        ));
    }
    sync_file_and_parent(path)
}

pub(crate) fn write_manifest_if_unchanged(
    path: &Path,
    expected: &FileSnapshot,
    manifest: &GenerationManifest,
) -> Result<FileSnapshot, ManifestWriteError> {
    let bytes = serde_json::to_vec_pretty(manifest)
        .map_err(|_| ManifestWriteError::Write("编码配置失败".into()))?;
    write_bytes_if_unchanged_classified(path, expected, &bytes)
}

pub(crate) fn read_manifest(path: &Path) -> Result<GenerationManifest, String> {
    read_json(path, "Friend generation manifest")?
        .ok_or_else(|| recovery_required("generation manifest 缺失"))
}

pub(crate) fn validate_manifest_identity(
    manifest: &GenerationManifest,
    generation_id: &str,
    profile_path: &Path,
    manifest_path: &Path,
) -> Result<(), String> {
    if manifest.schema_version != MANIFEST_SCHEMA_VERSION
        || manifest.generation_id != generation_id
        || manifest.owner != FRIEND_OWNER
        || manifest.product != FRIEND_PRODUCT
        || manifest.profile_path != profile_path.to_string_lossy()
        || manifest.manifest_path != manifest_path.to_string_lossy()
    {
        return Err(recovery_required("generation ownership proof 不匹配"));
    }
    Ok(())
}

pub(crate) fn profile_is_owned(profile: &Value, generation_id: &str) -> bool {
    profile
        .get("friend")
        .and_then(Value::as_object)
        .and_then(|friend| {
            Some(
                friend.get("owner")?.as_str()? == FRIEND_OWNER
                    && friend.get("product")?.as_str()? == FRIEND_PRODUCT
                    && friend.get("generation_id")?.as_str()? == generation_id
                    && friend.get("manifest_version")?.as_u64()?
                        == u64::from(MANIFEST_SCHEMA_VERSION),
            )
        })
        .unwrap_or(false)
}

pub(crate) fn profile_is_friend(profile: &Value) -> bool {
    profile
        .get("friend")
        .and_then(Value::as_object)
        .map(|friend| {
            friend.get("owner").and_then(Value::as_str) == Some(FRIEND_OWNER)
                && friend.get("product").and_then(Value::as_str) == Some(FRIEND_PRODUCT)
        })
        .unwrap_or(false)
}

pub(crate) fn metadata_entry_is_owned(entry: &Value, generation_id: &str) -> bool {
    entry.get("friend_owner").and_then(Value::as_str) == Some(FRIEND_OWNER)
        && entry.get("product").and_then(Value::as_str) == Some(FRIEND_PRODUCT)
        && entry.get("friend_generation_id").and_then(Value::as_str) == Some(generation_id)
}

pub(crate) fn metadata_entry_is_friend(entry: &Value) -> bool {
    entry.get("friend_owner").and_then(Value::as_str) == Some(FRIEND_OWNER)
        && entry.get("product").and_then(Value::as_str) == Some(FRIEND_PRODUCT)
}

pub(crate) fn manifest_cleanup_is_complete(manifest: &GenerationManifest) -> bool {
    matches!(manifest.delete_state, DeleteState::Deleted)
        && manifest.parent_generation_id.is_none()
        && !matches!(manifest.phase, Phase::DeleteOldFriendGeneration)
}

pub(crate) fn sha256_hex(data: &[u8]) -> String {
    let digest = Sha256::digest(data);
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn ownership_helpers_reject_unknown_and_user_objects() {
        let owned = json!({
            "friend": {
                "owner": FRIEND_OWNER,
                "product": FRIEND_PRODUCT,
                "generation_id": "gen-1",
                "manifest_version": MANIFEST_SCHEMA_VERSION
            }
        });
        let user = json!({"friend": {"owner": "user", "generation_id": "gen-1"}});
        assert!(profile_is_owned(&owned, "gen-1"));
        assert!(!profile_is_owned(&owned, "gen-2"));
        assert!(!profile_is_owned(&user, "gen-1"));
        assert!(metadata_entry_is_owned(
            &json!({
                "friend_owner": FRIEND_OWNER,
                "product": FRIEND_PRODUCT,
                "friend_generation_id": "gen-1"
            }),
            "gen-1"
        ));
    }

    #[test]
    fn snapshot_hash_is_non_sensitive_and_stable() {
        let snapshot = FileSnapshot {
            exists: true,
            bytes: b"metadata".to_vec(),
        };
        assert_eq!(
            snapshot.sha256().as_deref(),
            Some("45447b7afbd5e544f7d0f1df0fccd26014d9850130abd3f020b89ff96b82079f")
        );
    }

    #[test]
    fn unchanged_snapshot_check_refuses_external_change() {
        let root = std::env::temp_dir().join("friend-agent-recovery-unchanged-test");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).expect("test root");
        let path = root.join("metadata.json");
        fs::write(&path, b"before").expect("before");
        let expected = snapshot(&path, "expected").expect("snapshot");
        fs::write(&path, b"external").expect("external change");

        let error = write_bytes_if_unchanged(&path, &expected, b"replacement")
            .expect_err("unchanged-snapshot check must stop on external change");
        assert!(error.starts_with("RECOVERY_REQUIRED:"));
        assert_eq!(fs::read(&path).expect("current"), b"external");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn sensitive_profile_write_has_no_tmp_or_backup_plaintext_copy() {
        let root = std::env::temp_dir().join("friend-agent-sensitive-profile-test");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).expect("test root");
        let path = root.join("generation.json");
        let profile = json!({"inferenceGatewayApiKey": "test-secret"});

        write_sensitive_profile(&path, &profile).expect("sensitive profile");
        assert!(path.exists());
        assert!(!path.with_extension("friend-agent.tmp").exists());
        assert!(!path.with_extension("friend-agent.bak").exists());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(&path)
                    .expect("profile metadata")
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
        }

        let original = fs::read(&path).expect("profile bytes");
        assert!(write_sensitive_profile(&path, &json!({"key": "other"})).is_err());
        assert_eq!(fs::read(&path).expect("unchanged profile"), original);
        assert!(!path.with_extension("friend-agent.tmp").exists());
        assert!(!path.with_extension("friend-agent.bak").exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn sensitive_profile_cleanup_failure_is_recovery_required() {
        let root = std::env::temp_dir().join("friend-agent-sensitive-profile-cleanup-test");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).expect("test root");
        let path = root.join("generation.json");
        fs::write(&path, b"partial sensitive profile").expect("partial profile fixture");

        let error = cleanup_failed_sensitive_profile_with(&path, |_| {
            Err(io::Error::new(io::ErrorKind::PermissionDenied, "fixture"))
        })
        .expect_err("failed residue cleanup must be recovery required");
        assert!(error.starts_with("RECOVERY_REQUIRED:"));
        assert!(error.contains("残留"));
        assert_eq!(
            fs::read(&path).expect("residue"),
            b"partial sensitive profile"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn recovery_journal_is_identifiable_and_does_not_contain_secret_values() {
        let root = std::env::temp_dir().join("friend-agent-recovery-journal-test");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).expect("test root");
        let path = recovery_journal_path(&root);
        write_recovery_journal(
            &path,
            "generation-test",
            &Phase::WriteNewGeneration,
            Some("before-hash".into()),
            None,
            "pre-commit compensation failed",
        )
        .expect("journal");
        let text = fs::read_to_string(&path).expect("journal text");
        assert!(text.contains("RECOVERY_REQUIRED"));
        assert!(!text.contains("test-secret"));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn claude_transaction_lock_is_exclusive_within_the_process() {
        let root = std::env::temp_dir().join("friend-agent-transaction-lock-test");
        let _ = fs::remove_dir_all(&root);
        let guard = lock_claude_transaction(&root).expect("first transaction lock");
        let lock = CLAUDE_TRANSACTION_LOCK.get().expect("initialized lock");
        assert!(lock.try_lock().is_err());
        drop(guard);
        let reacquired_guard = lock.lock().expect("released process lock");
        drop(reacquired_guard);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn cooperative_lock_blocks_a_second_handle_across_threads() {
        let root = std::env::temp_dir().join("friend-agent-transaction-lock-handle-test");
        let _ = fs::remove_dir_all(&root);
        let guard = lock_claude_transaction(&root).expect("first transaction lock");
        let lock_path = claude_transaction_lock_path(&root);
        assert_eq!(fs::metadata(&lock_path).expect("lock metadata").len(), 0);

        let second_path = lock_path.clone();
        let thread = std::thread::spawn(move || {
            let second = fs::OpenOptions::new()
                .read(true)
                .write(true)
                .open(second_path)
                .expect("second lock handle");
            second.try_lock_exclusive().is_err()
        });
        assert!(thread.join().expect("lock thread"));

        drop(guard);
        let third = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&lock_path)
            .expect("released lock handle");
        third
            .try_lock_exclusive()
            .expect("released lock must be available");
        third.unlock().expect("unlock");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn recovery_journal_check_fails_closed_when_a_location_cannot_be_inspected() {
        let root = std::env::temp_dir().join("friend-agent-recovery-journal-check-test");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).expect("test root");
        // A NUL byte is rejected by path APIs on both Unix and Windows, so this
        // fixture deterministically exercises an uninspectable location without
        // relying on platform-specific permission-bit behavior.
        let blocked_location = root.join("not-a-directory\0");

        let error = ensure_no_recovery_journal(&root, &blocked_location)
            .expect_err("uninspectable journal location must block");
        assert!(error.starts_with("RECOVERY_REQUIRED:"));
        let _ = fs::remove_dir_all(root);
    }
}
