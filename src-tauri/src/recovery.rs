use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{fs, io::ErrorKind, path::Path};

pub(crate) const FRIEND_OWNER: &str = "friend-agent";
pub(crate) const FRIEND_PRODUCT: &str = "claude";
pub(crate) const MANIFEST_SCHEMA_VERSION: u8 = 1;

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
    pub(crate) parent_generation_id: Option<String>,
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
    Ok(current.exists == expected.exists && current.bytes == expected.bytes)
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
    if fs::write(&temporary, data).is_err() {
        let _ = fs::remove_file(&temporary);
        return Err("写入临时配置失败".into());
    }

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = fs::metadata(path)
            .map(|metadata| metadata.permissions().mode())
            .unwrap_or(0o600);
        if fs::set_permissions(&temporary, fs::Permissions::from_mode(mode)).is_err() {
            let _ = fs::remove_file(&temporary);
            return Err("设置配置权限失败".into());
        }
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

pub(crate) fn restore_snapshot(path: &Path, snapshot: &FileSnapshot) -> Result<(), String> {
    if snapshot.exists {
        write_bytes_atomic(path, &snapshot.bytes)
    } else {
        match fs::remove_file(path) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == ErrorKind::NotFound => Ok(()),
            Err(_) => Err("恢复元数据失败".into()),
        }
    }
}

pub(crate) fn write_manifest(path: &Path, manifest: &GenerationManifest) -> Result<(), String> {
    write_json_atomic(path, manifest)
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

pub(crate) fn metadata_entry_is_owned(entry: &Value, generation_id: &str) -> bool {
    entry.get("friend_owner").and_then(Value::as_str) == Some(FRIEND_OWNER)
        && entry.get("product").and_then(Value::as_str) == Some(FRIEND_PRODUCT)
        && entry.get("friend_generation_id").and_then(Value::as_str) == Some(generation_id)
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
}
