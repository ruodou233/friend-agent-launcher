use crate::{
    gateway::CatalogEntry,
    recovery::{
        self, CommitState, DeleteState, FileSnapshot, GenerationManifest, Phase,
        MANIFEST_SCHEMA_VERSION,
    },
    secure_store,
};
use serde_json::{json, Value};
use std::{
    env,
    path::{Path, PathBuf},
};

#[cfg(test)]
use std::cell::Cell;

const INSTALL_BINDING: &str = "claude-macos-v1a";
const FRIEND_PROFILE_FIELDS: &[&str] = &[
    "inferenceProvider",
    "inferenceCredentialKind",
    "inferenceGatewayBaseUrl",
    "inferenceGatewayApiKey",
    "inferenceGatewayAuthScheme",
    "inferenceModels",
    "disableDeploymentModeChooser",
    "friend",
];

const FAILPOINT_RESTORE_PROFILE_CLEANUP: u8 = 1;
const FAILPOINT_OLD_GENERATION_CLEANUP: u8 = 2;

#[cfg(test)]
thread_local! {
    static TEST_FAILPOINT: Cell<u8> = const { Cell::new(0) };
}

#[cfg(test)]
fn arm_test_failpoint(point: u8) {
    TEST_FAILPOINT.with(|failpoint| failpoint.set(point));
}

fn test_failpoint(point: u8) -> Result<(), String> {
    #[cfg(test)]
    {
        let injected = TEST_FAILPOINT.with(|failpoint| {
            if failpoint.get() == point {
                failpoint.set(0);
                true
            } else {
                false
            }
        });
        if injected {
            return Err(recovery::recovery_required("测试故障注入"));
        }
    }
    let _ = point;
    Ok(())
}

#[derive(Debug, Clone)]
pub(crate) struct ClaudePaths {
    pub(crate) library: PathBuf,
    pub(crate) metadata: PathBuf,
    pub(crate) manifest_dir: PathBuf,
}

pub(crate) fn default_paths() -> Result<ClaudePaths, String> {
    if !cfg!(target_os = "macos") {
        return Err("V1A Claude 配置仅支持 macOS".into());
    }
    let home = env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| "无法确定用户目录".to_string())?;
    Ok(paths_for_library(home.join(
        "Library/Application Support/Claude-3p/configLibrary",
    )))
}

pub(crate) fn paths_for_library(library: PathBuf) -> ClaudePaths {
    ClaudePaths {
        metadata: library.join("_meta.json"),
        manifest_dir: library.join("friend-generations"),
        library,
    }
}

fn profile_path(paths: &ClaudePaths, generation_id: &str) -> PathBuf {
    paths.library.join(format!("{generation_id}.json"))
}

fn manifest_path(paths: &ClaudePaths, generation_id: &str) -> PathBuf {
    paths.manifest_dir.join(format!("{generation_id}.json"))
}

fn read_metadata(paths: &ClaudePaths) -> Result<(Value, FileSnapshot), String> {
    let snapshot = recovery::snapshot(&paths.metadata, "Claude 配置库元数据")?;
    if !snapshot.exists {
        return Ok((json!({"appliedId": "", "entries": []}), snapshot));
    }
    let metadata: Value = serde_json::from_slice(&snapshot.bytes)
        .map_err(|_| recovery::recovery_required("Claude 元数据无法解析"))?;
    if !metadata.is_object() || !metadata.get("entries").is_some_and(Value::is_array) {
        return Err(recovery::recovery_required("Claude 元数据字段所有权不确定"));
    }
    Ok((metadata, snapshot))
}

fn entries(metadata: &Value) -> Result<&Vec<Value>, String> {
    metadata
        .get("entries")
        .and_then(Value::as_array)
        .ok_or_else(|| recovery::recovery_required("Claude 元数据 entries 不可验证"))
}

fn entries_mut(metadata: &mut Value) -> Result<&mut Vec<Value>, String> {
    metadata
        .get_mut("entries")
        .and_then(Value::as_array_mut)
        .ok_or_else(|| recovery::recovery_required("Claude 元数据 entries 不可写入"))
}

fn applied_id(metadata: &Value) -> Option<String> {
    metadata
        .get("appliedId")
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|value| !value.is_empty())
}

fn metadata_entry<'a>(metadata: &'a Value, generation_id: &str) -> Option<&'a Value> {
    entries(metadata)
        .ok()?
        .iter()
        .find(|entry| entry.get("id").and_then(Value::as_str) == Some(generation_id))
}

fn validate_recovery_anchor(
    paths: &ClaudePaths,
    metadata: &Value,
    anchor: Option<&str>,
) -> Result<(), String> {
    let Some(anchor) = anchor else {
        return Ok(());
    };
    if anchor.is_empty() || anchor.contains(['/', '\\']) {
        return Err(recovery::recovery_required("恢复锚点标识无效"));
    }
    let entry = metadata_entry(metadata, anchor)
        .ok_or_else(|| recovery::recovery_required("恢复锚点条目缺失"))?;
    if recovery::metadata_entry_is_friend(entry) {
        return Err(recovery::recovery_required(
            "恢复锚点不能指向 Friend generation",
        ));
    }
    let anchor_profile =
        recovery::read_json::<Value>(&profile_path(paths, anchor), "恢复锚点原始 profile")
            .map_err(|_| recovery::recovery_required("恢复锚点原始 profile 无法验证"))?
            .ok_or_else(|| recovery::recovery_required("恢复锚点原始 profile 缺失"))?;
    if !anchor_profile.is_object() || recovery::profile_is_friend(&anchor_profile) {
        return Err(recovery::recovery_required(
            "恢复锚点不是可验证的原始 profile",
        ));
    }
    Ok(())
}

fn owned_manifest(
    paths: &ClaudePaths,
    generation_id: &str,
    metadata_entry: &Value,
) -> Result<GenerationManifest, String> {
    if !recovery::metadata_entry_is_owned(metadata_entry, generation_id) {
        return Err(recovery::recovery_required("目标 generation 不属于 Friend"));
    }
    let expected_profile = profile_path(paths, generation_id);
    let expected_manifest = manifest_path(paths, generation_id);
    if metadata_entry.get("profile_path").and_then(Value::as_str)
        != Some(expected_profile.to_string_lossy().as_ref())
    {
        return Err(recovery::recovery_required(
            "Friend profile 路径所有权不匹配",
        ));
    }
    let manifest = recovery::read_manifest(&expected_manifest)?;
    recovery::validate_manifest_identity(
        &manifest,
        generation_id,
        &expected_profile,
        &expected_manifest,
    )?;
    if !recovery::manifest_cleanup_is_complete(&manifest) {
        return Err(recovery::recovery_required(
            "Friend generation 清理尚未完成",
        ));
    }
    let profile: Value = recovery::read_json(&expected_profile, "Friend Claude profile")?
        .ok_or_else(|| recovery::recovery_required("Friend profile 缺失"))?;
    if !recovery::profile_is_owned(&profile, generation_id) {
        return Err(recovery::recovery_required("Friend profile 标记不匹配"));
    }
    let profile_hash = recovery::snapshot(&expected_profile, "Friend Claude profile")?.sha256();
    if manifest.profile_after_sha256.as_ref() != profile_hash.as_ref() {
        return Err(recovery::recovery_required("Friend profile 已被外部修改"));
    }
    Ok(manifest)
}

fn active_manifest(
    paths: &ClaudePaths,
    generation_id: &str,
    metadata_entry: &Value,
    metadata_snapshot: &FileSnapshot,
) -> Result<GenerationManifest, String> {
    let manifest = owned_manifest(paths, generation_id, metadata_entry)?;
    if manifest.metadata_after_sha256.as_ref() != metadata_snapshot.sha256().as_ref() {
        return Err(recovery::recovery_required(
            "当前 Friend generation 的 metadata_after_sha256 与当前元数据不匹配",
        ));
    }
    Ok(manifest)
}

fn profile_for(
    entry: &CatalogEntry,
    generation_id: &str,
    gateway_url: &str,
    secret: &str,
) -> Value {
    json!({
        "friend": {
            "owner": recovery::FRIEND_OWNER,
            "product": recovery::FRIEND_PRODUCT,
            "generation_id": generation_id,
            "manifest_version": MANIFEST_SCHEMA_VERSION
        },
        "inferenceProvider": "gateway",
        "inferenceCredentialKind": "static",
        "inferenceGatewayBaseUrl": gateway_url.trim_end_matches('/'),
        "inferenceGatewayApiKey": secret,
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceModels": [{"name": entry.model_ref}],
        "disableDeploymentModeChooser": true
    })
}

fn verify_profile(
    profile: &Value,
    entry: &CatalogEntry,
    generation_id: &str,
    gateway_url: &str,
    secret: &str,
) -> Result<(), String> {
    if !recovery::profile_is_owned(profile, generation_id)
        || profile.get("inferenceProvider").and_then(Value::as_str) != Some("gateway")
        || profile
            .get("inferenceCredentialKind")
            .and_then(Value::as_str)
            != Some("static")
        || profile
            .get("inferenceGatewayBaseUrl")
            .and_then(Value::as_str)
            != Some(gateway_url.trim_end_matches('/'))
        || profile
            .get("inferenceGatewayAuthScheme")
            .and_then(Value::as_str)
            != Some("bearer")
        || profile
            .get("inferenceGatewayApiKey")
            .and_then(Value::as_str)
            != Some(secret)
        || profile
            .get("inferenceModels")
            .and_then(Value::as_array)
            .and_then(|models| models.first())
            .and_then(|model| model.get("name"))
            .and_then(Value::as_str)
            != Some(entry.model_ref.as_str())
    {
        return Err(recovery::recovery_required("Friend profile 读回校验失败"));
    }
    Ok(())
}

fn generation_entry(paths: &ClaudePaths, generation_id: &str) -> Value {
    json!({
        "id": generation_id,
        "name": "Friend Gateway",
        "friend_owner": recovery::FRIEND_OWNER,
        "friend_generation_id": generation_id,
        "product": recovery::FRIEND_PRODUCT,
        "profile_path": profile_path(paths, generation_id).to_string_lossy()
    })
}

fn new_manifest(
    paths: &ClaudePaths,
    generation_id: &str,
    parent_generation_id: Option<String>,
    recovery_anchor_applied_id: Option<String>,
    catalog_version: &str,
    metadata_snapshot: &FileSnapshot,
) -> GenerationManifest {
    GenerationManifest {
        schema_version: MANIFEST_SCHEMA_VERSION,
        generation_id: generation_id.to_string(),
        parent_generation_id,
        previous_applied_id: recovery_anchor_applied_id,
        profile_path: profile_path(paths, generation_id).to_string_lossy().into(),
        manifest_path: manifest_path(paths, generation_id).to_string_lossy().into(),
        metadata_path: paths.metadata.to_string_lossy().into(),
        owner: recovery::FRIEND_OWNER.into(),
        product: recovery::FRIEND_PRODUCT.into(),
        install_binding: INSTALL_BINDING.into(),
        field_set: FRIEND_PROFILE_FIELDS
            .iter()
            .map(|field| (*field).into())
            .collect(),
        profile_before_sha256: None,
        profile_after_sha256: None,
        metadata_before_sha256: metadata_snapshot.sha256(),
        metadata_after_sha256: None,
        expected_catalog_version: catalog_version.into(),
        phase: Phase::Preflight,
        commit_state: CommitState::Pending,
        delete_state: DeleteState::NotStarted,
    }
}

fn remove_new_generation(
    paths: &ClaudePaths,
    generation_id: &str,
    manifest_file: &Path,
    expected_manifest: &FileSnapshot,
) -> Result<(), String> {
    let profile = profile_path(paths, generation_id);
    let profile_snapshot = recovery::snapshot(&profile, "Friend 新 profile")?;
    let manifest_snapshot = recovery::snapshot(manifest_file, "新 generation manifest")?;
    if !recovery::current_matches(manifest_file, expected_manifest)? {
        return Err(recovery::recovery_required(
            "新 generation manifest 在补偿前被外部修改，停止删除",
        ));
    }
    if manifest_snapshot.exists {
        let proof = recovery::read_manifest(manifest_file)?;
        recovery::validate_manifest_identity(&proof, generation_id, &profile, manifest_file)?;
        if profile_snapshot.exists {
            let value: Value = recovery::read_json(&profile, "Friend 新 profile")?
                .ok_or_else(|| recovery::recovery_required("Friend 新 profile 消失"))?;
            if !recovery::profile_is_owned(&value, generation_id) {
                return Err(recovery::recovery_required(
                    "新 profile 所有权不确定，停止删除",
                ));
            }
            if proof.profile_after_sha256.as_ref() != profile_snapshot.sha256().as_ref() {
                return Err(recovery::recovery_required(
                    "新 profile 已被外部修改，停止删除",
                ));
            }
            recovery::remove_file_if_unchanged(&profile, &profile_snapshot, "新 Friend profile")?;
        }
    } else if profile_snapshot.exists {
        return Err(recovery::recovery_required(
            "新 profile 缺少 generation ownership proof，停止删除",
        ));
    }
    recovery::remove_file_if_unchanged(manifest_file, expected_manifest, "新 generation manifest")
}

fn rollback_before_commit(
    paths: &ClaudePaths,
    generation_id: &str,
    manifest_file: &Path,
    expected_manifest: &FileSnapshot,
    metadata_before: &FileSnapshot,
    metadata_after: Option<&FileSnapshot>,
) -> Result<(), String> {
    if let Some(after) = metadata_after {
        recovery::restore_snapshot_if_unchanged(&paths.metadata, after, metadata_before)?;
    } else if !recovery::current_matches(&paths.metadata, metadata_before)? {
        return Err(recovery::recovery_required("元数据在补偿前发生未知修改"));
    }
    remove_new_generation(paths, generation_id, manifest_file, expected_manifest)
}

fn write_recovery_journal(
    paths: &ClaudePaths,
    generation_id: &str,
    phase: &Phase,
    metadata_before: &FileSnapshot,
    metadata_after: Option<&FileSnapshot>,
    reason: &str,
) -> Result<(), String> {
    let primary = recovery::recovery_journal_path(&paths.library);
    let primary_result = recovery::write_recovery_journal(
        &primary,
        generation_id,
        phase,
        metadata_before.sha256(),
        metadata_after.and_then(FileSnapshot::sha256),
        reason,
    );

    let fallback = recovery::recovery_journal_path(&paths.manifest_dir);
    let fallback_result = if fallback == primary {
        Ok(())
    } else {
        recovery::write_recovery_journal(
            &fallback,
            generation_id,
            phase,
            metadata_before.sha256(),
            metadata_after.and_then(FileSnapshot::sha256),
            reason,
        )
    };

    match (primary_result, fallback_result) {
        (Ok(()), Ok(())) => Ok(()),
        (Err(_), Err(_)) => Err(recovery::recovery_required(
            "RECOVERY_REQUIRED journal 主、副路径均无法持久化",
        )),
        (Err(_), Ok(())) => Err(recovery::recovery_required(
            "RECOVERY_REQUIRED journal 主路径无法持久化",
        )),
        (Ok(()), Err(_)) => Err(recovery::recovery_required(
            "RECOVERY_REQUIRED journal 副路径无法持久化",
        )),
    }
}

fn persist_recovery_state(
    paths: &ClaudePaths,
    generation_id: &str,
    phase: &Phase,
    metadata_before: &FileSnapshot,
    metadata_after: Option<&FileSnapshot>,
    reason: &str,
) -> String {
    if write_recovery_journal(
        paths,
        generation_id,
        phase,
        metadata_before,
        metadata_after,
        reason,
    )
    .is_ok()
    {
        recovery::recovery_required(reason)
    } else {
        recovery::recovery_required("无法写入 RECOVERY_REQUIRED journal，必须人工恢复")
    }
}

fn mark_cleanup_failure(
    paths: &ClaudePaths,
    manifest_file: &Path,
    manifest: &mut GenerationManifest,
    manifest_snapshot: &mut FileSnapshot,
    metadata_before: &FileSnapshot,
    metadata_after: Option<&FileSnapshot>,
    reason: &str,
) -> String {
    manifest.delete_state = DeleteState::RecoveryRequired;
    manifest.metadata_after_sha256 = metadata_after.and_then(FileSnapshot::sha256);
    let manifest_write =
        match recovery::write_manifest_if_unchanged(manifest_file, manifest_snapshot, manifest) {
            Ok(snapshot) => {
                *manifest_snapshot = snapshot;
                Ok(())
            }
            Err(error) => Err(error),
        };
    let journal_write = write_recovery_journal(
        paths,
        &manifest.generation_id,
        &manifest.phase,
        metadata_before,
        metadata_after,
        reason,
    );

    match (manifest_write, journal_write) {
        (Ok(()), Ok(())) => recovery::recovery_required(reason),
        (Err(recovery::ManifestWriteError::Conflict(_)), Ok(())) => recovery::recovery_required(
            "Friend cleanup 状态 manifest 冲突（外部内容已保留），双路径 RECOVERY_REQUIRED journal 写入成功",
        ),
        (Err(recovery::ManifestWriteError::Write(_)), Ok(())) => recovery::recovery_required(
            "Friend cleanup 状态 manifest 写入失败，双路径 RECOVERY_REQUIRED journal 写入成功",
        ),
        (Ok(()), Err(_)) => recovery::recovery_required(
            "Friend cleanup 状态 manifest 已写入，但双路径 RECOVERY_REQUIRED journal 写入失败",
        ),
        (Err(recovery::ManifestWriteError::Conflict(_)), Err(_)) => {
            recovery::recovery_required(
                "Friend cleanup 状态 manifest 冲突（外部内容已保留），且双路径 RECOVERY_REQUIRED journal 写入失败",
            )
        }
        (Err(recovery::ManifestWriteError::Write(_)), Err(_)) => recovery::recovery_required(
            "Friend cleanup 状态 manifest 写入失败，且双路径 RECOVERY_REQUIRED journal 写入失败",
        ),
    }
}

#[derive(Debug)]
struct MetadataCleanupFailure {
    metadata_after: Option<FileSnapshot>,
}

impl MetadataCleanupFailure {
    fn known(metadata_after: FileSnapshot) -> Self {
        Self {
            metadata_after: Some(metadata_after),
        }
    }

    fn unknown() -> Self {
        Self {
            metadata_after: None,
        }
    }
}

fn fail_before_commit<Stop>(
    paths: &ClaudePaths,
    generation_id: &str,
    manifest_file: &Path,
    expected_manifest: &FileSnapshot,
    metadata_before: &FileSnapshot,
    metadata_after: Option<&FileSnapshot>,
    phase: Phase,
    reason: &str,
    stop_before_compensation: &Stop,
) -> String
where
    Stop: Fn() -> Result<(), String>,
{
    if stop_before_compensation().is_err() {
        return if write_recovery_journal(
            paths,
            generation_id,
            &phase,
            metadata_before,
            metadata_after,
            "official app could not be stopped before compensation",
        )
        .is_ok()
        {
            recovery::recovery_required("官方 App 无法退出，已写入 RECOVERY_REQUIRED journal")
        } else {
            recovery::recovery_required("官方 App 无法退出，且 RECOVERY_REQUIRED journal 无法写入")
        };
    }
    match rollback_before_commit(
        paths,
        generation_id,
        manifest_file,
        expected_manifest,
        metadata_before,
        metadata_after,
    ) {
        Ok(()) => reason.into(),
        Err(_) => {
            if write_recovery_journal(
                paths,
                generation_id,
                &phase,
                metadata_before,
                metadata_after,
                "pre-commit compensation failed",
            )
            .is_ok()
            {
                recovery::recovery_required("配置补偿失败，已写入 RECOVERY_REQUIRED journal")
            } else {
                recovery::recovery_required("配置补偿失败，且 RECOVERY_REQUIRED journal 无法写入")
            }
        }
    }
}

fn remove_parent_entry(
    paths: &ClaudePaths,
    generation_id: &str,
    expected_metadata: &FileSnapshot,
) -> Result<FileSnapshot, MetadataCleanupFailure> {
    let current = recovery::snapshot(&paths.metadata, "当前 Claude 元数据")
        .map_err(|_| MetadataCleanupFailure::unknown())?;
    if current.exists != expected_metadata.exists || current.bytes != expected_metadata.bytes {
        return Err(MetadataCleanupFailure::known(current));
    }
    let mut metadata: Value = serde_json::from_slice(&current.bytes)
        .map_err(|_| MetadataCleanupFailure::known(current.clone()))?;
    let entry = metadata_entry(&metadata, generation_id)
        .ok_or_else(|| MetadataCleanupFailure::known(current.clone()))?;
    owned_manifest(paths, generation_id, entry)
        .map_err(|_| MetadataCleanupFailure::known(current.clone()))?;
    entries_mut(&mut metadata)
        .map_err(|_| MetadataCleanupFailure::known(current.clone()))?
        .retain(|entry| entry.get("id").and_then(Value::as_str) != Some(generation_id));
    recovery::write_json_if_unchanged(&paths.metadata, expected_metadata, &metadata)
        .map_err(|_| MetadataCleanupFailure::unknown())
}

fn delete_old_generation(paths: &ClaudePaths, generation_id: &str) -> Result<(), String> {
    let manifest_path = manifest_path(paths, generation_id);
    let profile_path = profile_path(paths, generation_id);
    let manifest = recovery::read_manifest(&manifest_path)?;
    recovery::validate_manifest_identity(&manifest, generation_id, &profile_path, &manifest_path)?;
    let profile: Value = recovery::read_json(&profile_path, "旧 Friend profile")?
        .ok_or_else(|| recovery::recovery_required("旧 Friend profile 缺失"))?;
    if !recovery::profile_is_owned(&profile, generation_id) {
        return Err(recovery::recovery_required(
            "旧代 profile 所有权不确定，停止删除",
        ));
    }
    if manifest.profile_after_sha256.as_ref()
        != recovery::snapshot(&profile_path, "旧 Friend profile")?
            .sha256()
            .as_ref()
    {
        return Err(recovery::recovery_required(
            "旧代 profile 已被外部修改，停止删除",
        ));
    }
    test_failpoint(FAILPOINT_OLD_GENERATION_CLEANUP)?;
    let profile_snapshot = recovery::snapshot(&profile_path, "旧 Friend profile")?;
    let manifest_snapshot = recovery::snapshot(&manifest_path, "旧 generation manifest")?;
    // A legacy Keychain item is never deleted here: this V1A manifest does not
    // prove ownership of any pre-existing Keychain record.
    recovery::remove_file_if_unchanged(&profile_path, &profile_snapshot, "旧 Friend profile")?;
    recovery::remove_file_if_unchanged(&manifest_path, &manifest_snapshot, "旧 generation manifest")
}

pub(crate) fn configure<BeforeSnapshot, Launch, Stop>(
    paths: &ClaudePaths,
    entry: &CatalogEntry,
    catalog_version: &str,
    gateway_url: &str,
    secret: &str,
    before_snapshot: BeforeSnapshot,
    launch: Launch,
    stop_before_compensation: Stop,
) -> Result<(), String>
where
    BeforeSnapshot: FnOnce() -> Result<(), String>,
    Launch: FnOnce() -> Result<(), String>,
    Stop: Fn() -> Result<(), String>,
{
    let _transaction_lock = recovery::lock_claude_transaction(&paths.library)?;
    recovery::ensure_no_recovery_journal(&paths.library, &paths.manifest_dir)?;
    secure_store::validate_secret(secret)?;
    if gateway_url.trim().is_empty() {
        return Err(recovery::recovery_required("固定网关缺失"));
    }

    // This backend gate runs immediately before the first configuration
    // snapshot. The UI confirmation is only advisory and cannot replace it.
    before_snapshot()?;

    let (metadata, metadata_before) = read_metadata(paths)?;
    let current_applied_id = applied_id(&metadata);
    let (parent_generation_id, recovery_anchor_applied_id) =
        if let Some(current_id) = current_applied_id.as_deref() {
            match metadata_entry(&metadata, current_id) {
                Some(entry) if recovery::metadata_entry_is_owned(entry, current_id) => {
                    let parent = active_manifest(paths, current_id, entry, &metadata_before)?;
                    if parent.commit_state != CommitState::Committed {
                        return Err(recovery::recovery_required("旧 Friend 代尚未提交"));
                    }
                    (
                        Some(current_id.to_string()),
                        parent.previous_applied_id.clone(),
                    )
                }
                Some(_) | None => (None, current_applied_id.clone()),
            }
        } else {
            (None, None)
        };
    validate_recovery_anchor(paths, &metadata, recovery_anchor_applied_id.as_deref())?;

    let generation_id = secure_store::new_opaque_id("generation")?;
    let profile = profile_path(paths, &generation_id);
    let manifest_file = manifest_path(paths, &generation_id);
    let mut manifest_snapshot = recovery::snapshot(&manifest_file, "新 generation manifest")?;
    if profile.exists() || manifest_snapshot.exists {
        return Err(recovery::recovery_required("generation 标识冲突"));
    }
    let mut metadata_after: Option<FileSnapshot> = None;
    let mut manifest = new_manifest(
        paths,
        &generation_id,
        parent_generation_id.clone(),
        recovery_anchor_applied_id,
        catalog_version,
        &metadata_before,
    );

    macro_rules! precommit {
        ($operation:expr) => {
            match $operation {
                Ok(value) => value,
                Err(_) => {
                    return Err(fail_before_commit(
                        paths,
                        &generation_id,
                        &manifest_file,
                        &manifest_snapshot,
                        &metadata_before,
                        metadata_after.as_ref(),
                        manifest.phase.clone(),
                        "Friend Claude 配置未提交",
                        &stop_before_compensation,
                    ));
                }
            }
        };
    }

    manifest_snapshot = precommit!(recovery::write_manifest_if_unchanged(
        &manifest_file,
        &manifest_snapshot,
        &manifest,
    ));

    manifest.phase = Phase::OwnershipCapture;
    manifest_snapshot = precommit!(recovery::write_manifest_if_unchanged(
        &manifest_file,
        &manifest_snapshot,
        &manifest,
    ));
    manifest.phase = Phase::WriteNewGeneration;
    manifest_snapshot = precommit!(recovery::write_manifest_if_unchanged(
        &manifest_file,
        &manifest_snapshot,
        &manifest,
    ));

    let new_profile = profile_for(entry, &generation_id, gateway_url, secret);
    precommit!(recovery::write_sensitive_profile(&profile, &new_profile));
    let read_back_profile: Value =
        precommit!(
            recovery::read_json::<Value>(&profile, "Friend 新 profile").and_then(|profile| {
                profile.ok_or_else(|| recovery::recovery_required("Friend 新 profile 读回失败"))
            })
        );
    if verify_profile(
        &read_back_profile,
        entry,
        &generation_id,
        gateway_url,
        secret,
    )
    .is_err()
    {
        return Err(fail_before_commit(
            paths,
            &generation_id,
            &manifest_file,
            &manifest_snapshot,
            &metadata_before,
            metadata_after.as_ref(),
            manifest.phase.clone(),
            "Friend profile 读回校验失败，配置未提交",
            &stop_before_compensation,
        ));
    }
    manifest.profile_after_sha256 = precommit!(
        recovery::snapshot(&profile, "Friend 新 profile").map(|snapshot| snapshot.sha256())
    );
    manifest.phase = Phase::ReadbackVerify;
    manifest_snapshot = precommit!(recovery::write_manifest_if_unchanged(
        &manifest_file,
        &manifest_snapshot,
        &manifest,
    ));

    if !precommit!(recovery::current_matches(&paths.metadata, &metadata_before)) {
        return Err(fail_before_commit(
            paths,
            &generation_id,
            &manifest_file,
            &manifest_snapshot,
            &metadata_before,
            metadata_after.as_ref(),
            manifest.phase.clone(),
            "元数据在切换前被修改，配置未提交",
            &stop_before_compensation,
        ));
    }
    let mut switched_metadata = metadata.clone();
    match entries_mut(&mut switched_metadata) {
        Ok(entries) => entries.push(generation_entry(paths, &generation_id)),
        Err(_) => {
            return Err(fail_before_commit(
                paths,
                &generation_id,
                &manifest_file,
                &manifest_snapshot,
                &metadata_before,
                metadata_after.as_ref(),
                manifest.phase.clone(),
                "Claude 元数据不可写入，配置未提交",
                &stop_before_compensation,
            ));
        }
    }
    switched_metadata["appliedId"] = Value::String(generation_id.clone());
    metadata_after = Some(precommit!(recovery::write_json_if_unchanged(
        &paths.metadata,
        &metadata_before,
        &switched_metadata
    )));
    if !precommit!(recovery::current_matches(
        &paths.metadata,
        metadata_after
            .as_ref()
            .expect("metadata transaction snapshot")
    )) {
        return Err(fail_before_commit(
            paths,
            &generation_id,
            &manifest_file,
            &manifest_snapshot,
            &metadata_before,
            metadata_after.as_ref(),
            manifest.phase.clone(),
            "Claude 元数据写入后被外部修改，配置未提交",
            &stop_before_compensation,
        ));
    }
    let read_back_metadata: Value = precommit!(recovery::read_json::<Value>(
        &paths.metadata,
        "Claude 切换后元数据"
    )
    .and_then(|value| {
        value.ok_or_else(|| recovery::recovery_required("Claude 元数据读回失败"))
    }));
    let read_back_entry_is_owned = metadata_entry(&read_back_metadata, &generation_id)
        .map(|entry| recovery::metadata_entry_is_owned(entry, &generation_id))
        .unwrap_or(false);
    if applied_id(&read_back_metadata).as_deref() != Some(generation_id.as_str())
        || !read_back_entry_is_owned
    {
        return Err(fail_before_commit(
            paths,
            &generation_id,
            &manifest_file,
            &manifest_snapshot,
            &metadata_before,
            metadata_after.as_ref(),
            manifest.phase.clone(),
            "Claude 元数据读回校验失败，配置未提交",
            &stop_before_compensation,
        ));
    }
    manifest.metadata_after_sha256 = metadata_after.as_ref().and_then(FileSnapshot::sha256);
    manifest.phase = Phase::MetadataSwitch;
    manifest_snapshot = precommit!(recovery::write_manifest_if_unchanged(
        &manifest_file,
        &manifest_snapshot,
        &manifest,
    ));

    manifest.phase = Phase::OfficialAppVerify;
    manifest_snapshot = precommit!(recovery::write_manifest_if_unchanged(
        &manifest_file,
        &manifest_snapshot,
        &manifest,
    ));
    if launch().is_err() {
        return Err(fail_before_commit(
            paths,
            &generation_id,
            &manifest_file,
            &manifest_snapshot,
            &metadata_before,
            metadata_after.as_ref(),
            manifest.phase.clone(),
            "官方 App 启动失败，配置未提交",
            &stop_before_compensation,
        ));
    }

    manifest.phase = Phase::Commit;
    manifest.commit_state = CommitState::Committed;
    match recovery::write_manifest_if_unchanged(&manifest_file, &manifest_snapshot, &manifest) {
        Ok(snapshot) => manifest_snapshot = snapshot,
        Err(_) => {
            return Err(fail_before_commit(
                paths,
                &generation_id,
                &manifest_file,
                &manifest_snapshot,
                &metadata_before,
                metadata_after.as_ref(),
                manifest.phase.clone(),
                "COMMIT 记录失败，配置未提交",
                &stop_before_compensation,
            ));
        }
    }

    if let Some(parent_id) = parent_generation_id {
        manifest.phase = Phase::DeleteOldFriendGeneration;
        match recovery::write_manifest_if_unchanged(&manifest_file, &manifest_snapshot, &manifest) {
            Ok(snapshot) => manifest_snapshot = snapshot,
            Err(_) => {
                return Err(mark_cleanup_failure(
                    paths,
                    &manifest_file,
                    &mut manifest,
                    &mut manifest_snapshot,
                    &metadata_before,
                    metadata_after.as_ref(),
                    "旧 Friend 代清理失败",
                ));
            }
        }
        let cleaned_metadata = match remove_parent_entry(
            paths,
            &parent_id,
            metadata_after
                .as_ref()
                .expect("metadata transaction snapshot"),
        ) {
            Ok(snapshot) => snapshot,
            Err(failure) => {
                return Err(mark_cleanup_failure(
                    paths,
                    &manifest_file,
                    &mut manifest,
                    &mut manifest_snapshot,
                    &metadata_before,
                    failure.metadata_after.as_ref(),
                    "旧 Friend 代清理失败",
                ));
            }
        };
        if delete_old_generation(paths, &parent_id).is_err() {
            return Err(mark_cleanup_failure(
                paths,
                &manifest_file,
                &mut manifest,
                &mut manifest_snapshot,
                &metadata_before,
                Some(&cleaned_metadata),
                "旧 Friend 代清理失败",
            ));
        }
        metadata_after = Some(cleaned_metadata);
        manifest.parent_generation_id = None;
        manifest.metadata_after_sha256 = metadata_after.as_ref().and_then(FileSnapshot::sha256);
        manifest.phase = Phase::Commit;
    }
    manifest.delete_state = DeleteState::Deleted;
    match recovery::write_manifest_if_unchanged(&manifest_file, &manifest_snapshot, &manifest) {
        Ok(snapshot) => manifest_snapshot = snapshot,
        Err(_) => {
            return Err(mark_cleanup_failure(
                paths,
                &manifest_file,
                &mut manifest,
                &mut manifest_snapshot,
                &metadata_before,
                metadata_after.as_ref(),
                "Friend generation 清理状态无法持久化",
            ));
        }
    }
    let _ = manifest_snapshot;
    Ok(())
}

pub(crate) fn current_friend_key(paths: &ClaudePaths, gateway_url: &str) -> Result<String, String> {
    let _transaction_lock = recovery::lock_claude_transaction(&paths.library)?;
    recovery::ensure_no_recovery_journal(&paths.library, &paths.manifest_dir)?;
    let (metadata, metadata_snapshot) = read_metadata(paths)?;
    let current_id =
        applied_id(&metadata).ok_or_else(|| "未找到 Friend 自有 Claude profile".to_string())?;
    let entry = metadata_entry(&metadata, &current_id)
        .ok_or_else(|| "未找到 Friend 自有 Claude profile".to_string())?;
    let manifest = active_manifest(paths, &current_id, entry, &metadata_snapshot)?;
    if manifest.commit_state != CommitState::Committed {
        return Err(recovery::recovery_required("当前 Friend 代未提交"));
    }
    let path = profile_path(paths, &current_id);
    let profile: Value = recovery::read_json(&path, "当前 Friend profile")?
        .ok_or_else(|| recovery::recovery_required("当前 Friend profile 缺失"))?;
    if profile
        .get("inferenceGatewayBaseUrl")
        .and_then(Value::as_str)
        != Some(gateway_url.trim_end_matches('/'))
    {
        return Err(recovery::recovery_required(
            "当前 Friend profile 网关不匹配",
        ));
    }
    let secret = profile
        .get("inferenceGatewayApiKey")
        .and_then(Value::as_str)
        .ok_or_else(|| recovery::recovery_required("当前 Friend profile 无静态 Key"))?;
    secure_store::validate_secret(secret)?;
    Ok(secret.to_string())
}

pub(crate) fn restore<BeforeSnapshot>(
    paths: &ClaudePaths,
    before_snapshot: BeforeSnapshot,
) -> Result<bool, String>
where
    BeforeSnapshot: FnOnce() -> Result<(), String>,
{
    let _transaction_lock = recovery::lock_claude_transaction(&paths.library)?;
    recovery::ensure_no_recovery_journal(&paths.library, &paths.manifest_dir)?;
    before_snapshot().map_err(|error| format!("恢复前未能停止官方 App：{error}"))?;
    let (metadata, metadata_before) = read_metadata(paths)?;
    let current_id = match applied_id(&metadata) {
        Some(id) => id,
        None => return Ok(false),
    };
    let entry = match metadata_entry(&metadata, &current_id) {
        Some(entry) if recovery::metadata_entry_is_owned(entry, &current_id) => entry,
        Some(_) | None => return Ok(false),
    };
    let manifest = active_manifest(paths, &current_id, entry, &metadata_before)?;
    if manifest.commit_state != CommitState::Committed {
        return Err(recovery::recovery_required("待提交 Friend 代不能自动恢复"));
    }
    validate_recovery_anchor(paths, &metadata, manifest.previous_applied_id.as_deref())?;

    let profile = profile_path(paths, &current_id);
    let profile_snapshot = recovery::snapshot(&profile, "当前 Friend profile")?;
    let profile_value: Value = recovery::read_json(&profile, "当前 Friend profile")?
        .ok_or_else(|| recovery::recovery_required("当前 Friend profile 缺失"))?;
    if !recovery::profile_is_owned(&profile_value, &current_id) {
        return Err(recovery::recovery_required(
            "恢复前发现 Friend profile 所有权变化",
        ));
    }
    if manifest.profile_after_sha256.as_ref() != profile_snapshot.sha256().as_ref() {
        return Err(recovery::recovery_required(
            "恢复前发现 Friend profile 已被外部修改",
        ));
    }
    let manifest_file = manifest_path(paths, &current_id);
    let manifest_snapshot = recovery::snapshot(&manifest_file, "当前 generation manifest")?;
    if !manifest_snapshot.exists {
        return Err(recovery::recovery_required("当前 generation manifest 缺失"));
    }
    if !recovery::current_matches(&paths.metadata, &metadata_before)?
        || !recovery::current_matches(&profile, &profile_snapshot)?
    {
        return Err(recovery::recovery_required(
            "恢复前发现配置已被用户或外部修改",
        ));
    }

    let mut restored = metadata.clone();
    entries_mut(&mut restored)
        .map_err(|_| recovery::recovery_required("恢复目标元数据不可写入"))?
        .retain(|item| item.get("id").and_then(Value::as_str) != Some(current_id.as_str()));
    let previous_applied_id = manifest.previous_applied_id.clone().unwrap_or_default();
    restored["appliedId"] = Value::String(previous_applied_id.clone());
    let metadata_after =
        match recovery::write_json_if_unchanged(&paths.metadata, &metadata_before, &restored) {
            Ok(snapshot) => snapshot,
            Err(_) => {
                return Err(persist_recovery_state(
                    paths,
                    &current_id,
                    &Phase::MetadataSwitch,
                    &metadata_before,
                    None,
                    "恢复元数据写回失败",
                ));
            }
        };

    let post_metadata_result = (|| -> Result<(), String> {
        let read_back: Value = recovery::read_json(&paths.metadata, "恢复后 Claude 元数据")?
            .ok_or_else(|| recovery::recovery_required("恢复后元数据读回失败"))?;
        if applied_id(&read_back).unwrap_or_default() != previous_applied_id
            || metadata_entry(&read_back, &current_id).is_some()
            || !recovery::current_matches(&paths.metadata, &metadata_after)?
            || !recovery::current_matches(&profile, &profile_snapshot)?
        {
            return Err(recovery::recovery_required(
                "恢复后发现配置被用户或外部修改",
            ));
        }

        test_failpoint(FAILPOINT_RESTORE_PROFILE_CLEANUP)?;
        recovery::remove_file_if_unchanged(&profile, &profile_snapshot, "当前 Friend profile")?;
        recovery::remove_file_if_unchanged(
            &manifest_file,
            &manifest_snapshot,
            "当前 generation manifest",
        )?;
        Ok(())
    })();
    if post_metadata_result.is_err() {
        return Err(persist_recovery_state(
            paths,
            &current_id,
            &Phase::MetadataSwitch,
            &metadata_before,
            Some(&metadata_after),
            "恢复后校验或 Friend 文件清理失败",
        ));
    }
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gateway::{
        resolve_entry, CatalogDocument, CatalogEntry, FIXED_GATEWAY_REF, PRODUCT, PROTOCOL,
    };
    use std::{cell::Cell, fs};

    fn fixture_entry() -> CatalogEntry {
        let catalog: CatalogDocument = serde_json::from_value(json!({
            "product": PRODUCT,
            "protocol": PROTOCOL,
            "catalog_version": "v1a-test-1",
            "expires_at": "2099-01-01T00:00:00Z",
            "integrity": "tls-fixed-gateway",
            "catalog": [{
                "product": PRODUCT,
                "protocol": PROTOCOL,
                "canonical_id": "claude.default",
                "model_ref": "friend-model:claude-test",
                "gateway_ref": FIXED_GATEWAY_REF,
                "display_name": "Claude 默认",
                "capabilities": ["streaming", "tool_use"],
                "default": true,
                "catalog_version": "v1a-test-1",
                "expires_at": "2099-01-01T00:00:00Z",
                "billing_label": "按量计费"
            }],
            "balance": {
                "amount_minor": 1250,
                "currency": "CNY",
                "as_of": "2099-01-01T00:00:00Z",
                "source": "new-api"
            }
        }))
        .expect("catalog fixture");
        resolve_entry(&catalog, "claude.default", "v1a-test-1")
            .expect("validated catalog entry")
            .clone()
    }

    fn temp_paths() -> ClaudePaths {
        let directory =
            std::env::temp_dir().join(secure_store::new_opaque_id("claude-test").expect("test id"));
        paths_for_library(directory.join("configLibrary"))
    }

    #[test]
    fn configure_preserves_unknown_profile_and_restore_only_removes_friend_generation() {
        let paths = temp_paths();
        fs::create_dir_all(&paths.library).expect("library");
        let user_profile = paths.library.join("user-profile.json");
        fs::write(&user_profile, br#"{"user":"keep"}"#).expect("user profile");
        recovery::write_json_atomic(
            &paths.metadata,
            &json!({
                "appliedId": "user-profile",
                "entries": [{"id": "user-profile", "name": "User profile"}],
                "keep": true
            }),
        )
        .expect("metadata");

        configure(
            &paths,
            &fixture_entry(),
            "v1a-test-1",
            "http://127.0.0.1:3000",
            "test-secret",
            || Ok(()),
            || Ok(()),
            || Ok(()),
        )
        .expect("configure");
        let (metadata, _) = read_metadata(&paths).expect("read metadata");
        let generation_id = applied_id(&metadata).expect("friend id");
        assert_eq!(metadata["keep"], true);
        assert_eq!(
            fs::read(&user_profile).expect("user profile"),
            br#"{"user":"keep"}"#
        );
        assert_eq!(
            current_friend_key(&paths, "http://127.0.0.1:3000").expect("read key"),
            "test-secret"
        );
        let manifest_text =
            fs::read_to_string(manifest_path(&paths, &generation_id)).expect("manifest");
        assert!(!manifest_text.contains("test-secret"));

        assert!(restore(&paths, || Ok(())).expect("restore"));
        let (restored, _) = read_metadata(&paths).expect("restored metadata");
        assert_eq!(restored["appliedId"], "user-profile");
        assert!(!profile_path(&paths, &generation_id).exists());
        assert!(fs::read(&user_profile).is_ok());
        let _ = fs::remove_dir_all(paths.library.parent().expect("parent"));
    }

    #[test]
    fn external_profile_change_requires_recovery_before_balance_or_restore() {
        let paths = temp_paths();
        fs::create_dir_all(&paths.library).expect("library");
        recovery::write_json_atomic(
            &paths.metadata,
            &json!({
                "appliedId": "",
                "entries": []
            }),
        )
        .expect("metadata");
        configure(
            &paths,
            &fixture_entry(),
            "v1a-test-1",
            "http://127.0.0.1:3000",
            "test-secret",
            || Ok(()),
            || Ok(()),
            || Ok(()),
        )
        .expect("configure");
        let (metadata, _) = read_metadata(&paths).expect("read metadata");
        let generation_id = applied_id(&metadata).expect("friend id");
        let profile = profile_path(&paths, &generation_id);
        let mut profile_value: Value = recovery::read_json(&profile, "profile")
            .expect("read")
            .expect("profile");
        profile_value["disableDeploymentModeChooser"] = Value::Bool(false);
        fs::write(
            &profile,
            serde_json::to_vec_pretty(&profile_value).expect("mutate fixture encoding"),
        )
        .expect("mutate fixture");

        let balance_error = current_friend_key(&paths, "http://127.0.0.1:3000")
            .expect_err("changed profile must not be read");
        assert!(balance_error.starts_with("RECOVERY_REQUIRED:"));
        assert!(restore(&paths, || Ok(())).is_err());
        let (still_current, _) = read_metadata(&paths).expect("metadata remains");
        assert_eq!(still_current["appliedId"], generation_id);
        let _ = fs::remove_dir_all(paths.library.parent().expect("parent"));
    }

    #[test]
    fn active_generation_metadata_hash_mismatch_blocks_key_and_rotation() {
        let paths = temp_paths();
        fs::create_dir_all(&paths.library).expect("library");
        recovery::write_json_atomic(&paths.metadata, &json!({"appliedId": "", "entries": []}))
            .expect("metadata");
        configure(
            &paths,
            &fixture_entry(),
            "v1a-test-1",
            "http://127.0.0.1:3000",
            "test-secret-active-hash",
            || Ok(()),
            || Ok(()),
            || Ok(()),
        )
        .expect("configure");

        let (mut metadata, _) = read_metadata(&paths).expect("metadata");
        metadata["external_change"] = Value::Bool(true);
        recovery::write_json_atomic(&paths.metadata, &metadata).expect("external metadata");

        assert!(current_friend_key(&paths, "http://127.0.0.1:3000").is_err());
        assert!(configure(
            &paths,
            &fixture_entry(),
            "v1a-test-1",
            "http://127.0.0.1:3000",
            "test-secret-rotation-blocked",
            || Ok(()),
            || Ok(()),
            || Ok(()),
        )
        .is_err());
        assert!(
            !recovery::recovery_journal_path(&paths.library).exists(),
            "preflight metadata mismatch must fail closed before creating a new generation"
        );
        let _ = fs::remove_dir_all(paths.library.parent().expect("parent"));
    }

    #[test]
    fn restore_uses_metadata_unchanged_check_and_does_not_overwrite_external_changes() {
        let paths = temp_paths();
        fs::create_dir_all(&paths.library).expect("library");
        recovery::write_json_atomic(
            &paths.metadata,
            &json!({
                "appliedId": "",
                "entries": []
            }),
        )
        .expect("metadata");
        configure(
            &paths,
            &fixture_entry(),
            "v1a-test-1",
            "http://127.0.0.1:3000",
            "test-secret",
            || Ok(()),
            || Ok(()),
            || Ok(()),
        )
        .expect("configure");

        let (mut changed, _) = read_metadata(&paths).expect("metadata");
        changed["external_change"] = Value::Bool(true);
        recovery::write_json_atomic(&paths.metadata, &changed).expect("external change");

        let error = restore(&paths, || Ok(())).expect_err("restore must stop on metadata change");
        assert!(error.starts_with("RECOVERY_REQUIRED:"));
        let (still_changed, _) = read_metadata(&paths).expect("metadata remains");
        assert_eq!(still_changed["external_change"], true);
        let _ = fs::remove_dir_all(paths.library.parent().expect("parent"));
    }

    #[test]
    fn failed_launch_rolls_back_new_friend_generation_without_touching_user_profile() {
        let paths = temp_paths();
        fs::create_dir_all(&paths.library).expect("library");
        let before = json!({
            "appliedId": "user-profile",
            "entries": [{"id": "user-profile", "name": "User profile"}]
        });
        recovery::write_json_atomic(&paths.metadata, &before).expect("metadata");
        let result = configure(
            &paths,
            &fixture_entry(),
            "v1a-test-1",
            "http://127.0.0.1:3000",
            "test-secret",
            || Ok(()),
            || Err("process not started".into()),
            || Ok(()),
        );
        assert!(result.is_err());
        let (after, _) = read_metadata(&paths).expect("after");
        assert_eq!(after, before);
        let remaining_profiles = fs::read_dir(&paths.library)
            .expect("library entries")
            .filter_map(Result::ok)
            .filter(|entry| {
                entry
                    .file_type()
                    .map(|kind| kind.is_file())
                    .unwrap_or(false)
            })
            .filter(|entry| {
                entry.file_name() != "_meta.json" && entry.file_name() != ".friend-agent.lock"
            })
            .collect::<Vec<_>>();
        assert!(remaining_profiles.is_empty());
        let _ = fs::remove_dir_all(paths.library.parent().expect("parent"));
    }

    #[test]
    fn multiple_successful_rotations_restore_original_state_without_dangling_references() {
        let paths = temp_paths();
        fs::create_dir_all(&paths.library).expect("library");
        let original_profile = paths.library.join("official-profile.json");
        fs::write(
            &original_profile,
            br#"{"provider":"official","user":"keep"}"#,
        )
        .expect("official profile");
        let original = json!({
            "appliedId": "official-profile",
            "entries": [{"id": "official-profile", "name": "Official profile"}],
            "keep": {"theme": "official"}
        });
        recovery::write_json_atomic(&paths.metadata, &original).expect("metadata");

        configure(
            &paths,
            &fixture_entry(),
            "v1a-test-1",
            "http://127.0.0.1:3000",
            "fixture-secret-one",
            || Ok(()),
            || Ok(()),
            || Ok(()),
        )
        .expect("first configure");
        let (after_first, _) = read_metadata(&paths).expect("first metadata");
        let first_id = applied_id(&after_first).expect("first Friend id");

        configure(
            &paths,
            &fixture_entry(),
            "v1a-test-1",
            "http://127.0.0.1:3000",
            "fixture-secret-two",
            || Ok(()),
            || Ok(()),
            || Ok(()),
        )
        .expect("second configure");
        let (after_second, _) = read_metadata(&paths).expect("second metadata");
        let second_id = applied_id(&after_second).expect("second Friend id");
        assert_ne!(first_id, second_id);
        assert_eq!(
            current_friend_key(&paths, "http://127.0.0.1:3000").expect("second key"),
            "fixture-secret-two"
        );
        let second_manifest =
            recovery::read_manifest(&manifest_path(&paths, &second_id)).expect("second manifest");
        assert_eq!(
            second_manifest.previous_applied_id.as_deref(),
            Some("official-profile")
        );
        assert!(second_manifest.parent_generation_id.is_none());
        assert_eq!(
            second_manifest.metadata_after_sha256,
            recovery::snapshot(&paths.metadata, "metadata")
                .expect("metadata snapshot")
                .sha256()
        );
        assert!(!profile_path(&paths, &first_id).exists());
        assert!(!manifest_path(&paths, &first_id).exists());

        assert!(restore(&paths, || Ok(())).expect("restore"));
        let (restored, _) = read_metadata(&paths).expect("restored metadata");
        assert_eq!(restored, original);
        assert_eq!(
            fs::read(&original_profile).expect("official profile"),
            br#"{"provider":"official","user":"keep"}"#
        );
        assert!(!profile_path(&paths, &second_id).exists());
        assert!(!manifest_path(&paths, &second_id).exists());
        let _ = fs::remove_dir_all(paths.library.parent().expect("parent"));
    }

    #[test]
    fn fallback_recovery_journal_blocks_all_claude_entrypoints() {
        let paths = temp_paths();
        fs::create_dir_all(&paths.library).expect("library");
        let fallback_journal = recovery::recovery_journal_path(&paths.manifest_dir);
        recovery::write_recovery_journal(
            &fallback_journal,
            "generation-fallback",
            &Phase::Commit,
            None,
            None,
            "fixture fallback journal",
        )
        .expect("fallback journal");

        let configure_error = configure(
            &paths,
            &fixture_entry(),
            "v1a-test-1",
            "http://127.0.0.1:3000",
            "fake-key-fallback",
            || Ok(()),
            || Ok(()),
            || Ok(()),
        )
        .expect_err("fallback journal must block configure");
        assert!(configure_error.starts_with("RECOVERY_REQUIRED:"));

        let balance_error = current_friend_key(&paths, "http://127.0.0.1:3000")
            .expect_err("fallback journal must block balance reads");
        assert!(balance_error.starts_with("RECOVERY_REQUIRED:"));

        let callback_called = Cell::new(false);
        let restore_error = restore(&paths, || {
            callback_called.set(true);
            Ok(())
        })
        .expect_err("fallback journal must block restore");
        assert!(restore_error.starts_with("RECOVERY_REQUIRED:"));
        assert!(!callback_called.get());
        let _ = fs::remove_dir_all(paths.library.parent().expect("parent"));
    }

    #[test]
    fn recovery_journal_requires_both_locations_to_persist() {
        let paths = temp_paths();
        fs::create_dir_all(&paths.library).expect("library");
        fs::create_dir_all(&paths.manifest_dir).expect("manifest directory");
        let metadata_before = FileSnapshot {
            exists: false,
            bytes: Vec::new(),
        };

        write_recovery_journal(
            &paths,
            "journal-both",
            &Phase::Commit,
            &metadata_before,
            None,
            "double journal fixture",
        )
        .expect("both journal locations");
        for path in [
            recovery::recovery_journal_path(&paths.library),
            recovery::recovery_journal_path(&paths.manifest_dir),
        ] {
            let text = fs::read_to_string(path).expect("journal");
            assert!(text.contains("RECOVERY_REQUIRED"));
            assert!(!text.contains("test-secret"));
        }
        let _ = fs::remove_dir_all(paths.library.parent().expect("parent"));
    }

    #[test]
    fn recovery_journal_partial_location_failure_is_not_a_success() {
        let root = std::env::temp_dir()
            .join(secure_store::new_opaque_id("claude-journal-partial").expect("test id"));
        let primary_file = root.join("library-file");
        let fallback_dir = root.join("manifest-dir");
        fs::create_dir_all(&root).expect("root");
        fs::write(&primary_file, b"not a directory").expect("primary blocker");
        fs::create_dir_all(&fallback_dir).expect("fallback directory");
        let paths = ClaudePaths {
            library: primary_file,
            metadata: root.join("metadata.json"),
            manifest_dir: fallback_dir.clone(),
        };
        let metadata_before = FileSnapshot {
            exists: false,
            bytes: Vec::new(),
        };

        assert!(write_recovery_journal(
            &paths,
            "journal-primary-fails",
            &Phase::Commit,
            &metadata_before,
            None,
            "primary failure fixture",
        )
        .is_err());
        assert!(recovery::recovery_journal_path(&fallback_dir).exists());

        let primary_dir = root.join("library-dir");
        let fallback_file = root.join("manifest-file");
        fs::create_dir_all(&primary_dir).expect("primary directory");
        fs::write(&fallback_file, b"not a directory").expect("fallback blocker");
        let paths = ClaudePaths {
            library: primary_dir.clone(),
            metadata: root.join("metadata-two.json"),
            manifest_dir: fallback_file,
        };
        assert!(write_recovery_journal(
            &paths,
            "journal-fallback-fails",
            &Phase::Commit,
            &metadata_before,
            None,
            "fallback failure fixture",
        )
        .is_err());
        assert!(recovery::recovery_journal_path(&primary_dir).exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn cleanup_failure_reports_manifest_write_failure_and_still_attempts_both_journals() {
        let paths = temp_paths();
        fs::create_dir_all(&paths.library).expect("library");
        fs::create_dir_all(&paths.manifest_dir).expect("manifest directory");
        let blocked_parent = paths.manifest_dir.join("manifest-parent-file");
        fs::write(&blocked_parent, b"not a directory").expect("manifest blocker");
        let manifest_file = blocked_parent.join("generation.json");
        let metadata_before = FileSnapshot {
            exists: false,
            bytes: Vec::new(),
        };
        let mut manifest = new_manifest(
            &paths,
            "cleanup-failure-generation",
            None,
            None,
            "v1a-test-1",
            &metadata_before,
        );
        manifest.phase = Phase::DeleteOldFriendGeneration;
        let mut manifest_snapshot = FileSnapshot {
            exists: false,
            bytes: Vec::new(),
        };

        let error = mark_cleanup_failure(
            &paths,
            &manifest_file,
            &mut manifest,
            &mut manifest_snapshot,
            &metadata_before,
            None,
            "cleanup failure fixture",
        );
        assert!(error.contains("manifest 写入失败"));
        assert_eq!(manifest.delete_state, DeleteState::RecoveryRequired);
        assert!(recovery::recovery_journal_path(&paths.library).exists());
        assert!(recovery::recovery_journal_path(&paths.manifest_dir).exists());
        assert!(!manifest_file.exists());
        let _ = fs::remove_dir_all(paths.library.parent().expect("parent"));
    }

    #[test]
    fn manifest_state_write_rejects_external_change_and_preserves_external_bytes() {
        let paths = temp_paths();
        fs::create_dir_all(&paths.library).expect("library");
        let manifest_file = manifest_path(&paths, "manifest-conflict");
        let expected_absent =
            recovery::snapshot(&manifest_file, "new manifest").expect("absent snapshot");
        let mut manifest = new_manifest(
            &paths,
            "manifest-conflict",
            None,
            None,
            "v1a-test-1",
            &expected_absent,
        );
        let expected_present =
            recovery::write_manifest_if_unchanged(&manifest_file, &expected_absent, &manifest)
                .expect("initial manifest");
        let external_bytes = br#"{"external":true}"#;
        fs::write(&manifest_file, external_bytes).expect("external manifest change");

        manifest.phase = Phase::OwnershipCapture;
        let error =
            recovery::write_manifest_if_unchanged(&manifest_file, &expected_present, &manifest)
                .expect_err("manifest conflict must reject next status write");
        assert!(matches!(error, recovery::ManifestWriteError::Conflict(_)));
        assert_eq!(
            fs::read(&manifest_file).expect("external manifest"),
            external_bytes
        );
        let _ = fs::remove_dir_all(paths.library.parent().expect("parent"));
    }

    #[test]
    fn cleanup_failure_manifest_conflict_preserves_external_bytes_and_writes_both_journals() {
        let paths = temp_paths();
        fs::create_dir_all(&paths.library).expect("library");
        fs::create_dir_all(&paths.manifest_dir).expect("manifest directory");
        let manifest_file = manifest_path(&paths, "cleanup-conflict");
        let expected_absent =
            recovery::snapshot(&manifest_file, "new cleanup manifest").expect("snapshot");
        let mut manifest = new_manifest(
            &paths,
            "cleanup-conflict",
            None,
            None,
            "v1a-test-1",
            &expected_absent,
        );
        let mut manifest_snapshot =
            recovery::write_manifest_if_unchanged(&manifest_file, &expected_absent, &manifest)
                .expect("initial manifest");
        manifest.phase = Phase::DeleteOldFriendGeneration;
        let external_bytes = br#"{"external":"preserve"}"#;
        fs::write(&manifest_file, external_bytes).expect("external manifest change");

        let metadata_before = FileSnapshot {
            exists: false,
            bytes: Vec::new(),
        };
        let error = mark_cleanup_failure(
            &paths,
            &manifest_file,
            &mut manifest,
            &mut manifest_snapshot,
            &metadata_before,
            None,
            "cleanup conflict fixture",
        );
        assert!(error.contains("manifest 冲突"));
        assert!(error.contains("journal 写入成功"));
        assert_eq!(
            fs::read(&manifest_file).expect("external manifest"),
            external_bytes
        );
        assert!(recovery::recovery_journal_path(&paths.library).exists());
        assert!(recovery::recovery_journal_path(&paths.manifest_dir).exists());
        let _ = fs::remove_dir_all(paths.library.parent().expect("parent"));
    }

    #[test]
    fn restore_post_metadata_cleanup_failure_persists_journal_and_blocks_followups() {
        let paths = temp_paths();
        fs::create_dir_all(&paths.library).expect("library");
        recovery::write_json_atomic(&paths.metadata, &json!({"appliedId": "", "entries": []}))
            .expect("metadata");
        configure(
            &paths,
            &fixture_entry(),
            "v1a-test-1",
            "http://127.0.0.1:3000",
            "fake-key-restore-cleanup",
            || Ok(()),
            || Ok(()),
            || Ok(()),
        )
        .expect("configure");
        let generation_id =
            applied_id(&read_metadata(&paths).expect("metadata").0).expect("generation id");

        arm_test_failpoint(FAILPOINT_RESTORE_PROFILE_CLEANUP);
        let restore_error = restore(&paths, || Ok(())).expect_err("cleanup must fail");
        assert!(restore_error.starts_with("RECOVERY_REQUIRED:"));
        let journal_text = fs::read_to_string(recovery::recovery_journal_path(&paths.library))
            .expect("recovery journal");
        assert!(journal_text.contains("RECOVERY_REQUIRED"));
        assert!(!journal_text.contains("fake-key-restore-cleanup"));
        assert!(
            recovery::recovery_journal_path(&paths.manifest_dir).exists(),
            "fallback journal must also be durable"
        );
        assert_eq!(
            applied_id(&read_metadata(&paths).expect("restored metadata").0),
            None
        );
        assert!(profile_path(&paths, &generation_id).exists());

        assert!(configure(
            &paths,
            &fixture_entry(),
            "v1a-test-1",
            "http://127.0.0.1:3000",
            "fake-key-after-restore-failure",
            || Ok(()),
            || Ok(()),
            || Ok(()),
        )
        .is_err());
        assert!(current_friend_key(&paths, "http://127.0.0.1:3000").is_err());
        assert!(restore(&paths, || Ok(())).is_err());
        let _ = fs::remove_dir_all(paths.library.parent().expect("parent"));
    }

    #[test]
    fn old_generation_cleanup_failure_persists_journal_and_manifest_guard_blocks_continuation() {
        let paths = temp_paths();
        fs::create_dir_all(&paths.library).expect("library");
        let original_profile = paths.library.join("official-profile.json");
        fs::write(&original_profile, br#"{"provider":"official"}"#).expect("official profile");
        recovery::write_json_atomic(
            &paths.metadata,
            &json!({
                "appliedId": "official-profile",
                "entries": [{"id": "official-profile", "name": "Official profile"}]
            }),
        )
        .expect("metadata");
        configure(
            &paths,
            &fixture_entry(),
            "v1a-test-1",
            "http://127.0.0.1:3000",
            "fake-key-old-one",
            || Ok(()),
            || Ok(()),
            || Ok(()),
        )
        .expect("first configure");
        let first_id = applied_id(&read_metadata(&paths).expect("first metadata").0)
            .expect("first generation id");

        arm_test_failpoint(FAILPOINT_OLD_GENERATION_CLEANUP);
        let rotation_error = configure(
            &paths,
            &fixture_entry(),
            "v1a-test-1",
            "http://127.0.0.1:3000",
            "fake-key-old-two",
            || Ok(()),
            || Ok(()),
            || Ok(()),
        )
        .expect_err("old generation cleanup must fail");
        assert!(rotation_error.starts_with("RECOVERY_REQUIRED:"));

        let (metadata_after_cleanup, metadata_snapshot) = read_metadata(&paths).expect("metadata");
        let second_id = applied_id(&metadata_after_cleanup).expect("second generation id");
        assert!(
            metadata_entry(&metadata_after_cleanup, &first_id).is_none(),
            "parent metadata entry must be removed before old generation deletion"
        );
        let manifest =
            recovery::read_manifest(&manifest_path(&paths, &second_id)).expect("second manifest");
        assert_eq!(manifest.delete_state, DeleteState::RecoveryRequired);
        let expected_metadata_hash = metadata_snapshot.sha256();
        assert_eq!(
            manifest.metadata_after_sha256,
            expected_metadata_hash.clone(),
            "manifest recovery state must describe metadata after parent removal"
        );
        let journal_path = recovery::recovery_journal_path(&paths.library);
        let journal_text = fs::read_to_string(&journal_path).expect("recovery journal");
        assert!(journal_text.contains("RECOVERY_REQUIRED"));
        assert!(!journal_text.contains("fake-key-old-one"));
        assert!(!journal_text.contains("fake-key-old-two"));
        for journal_path in [
            recovery::recovery_journal_path(&paths.library),
            recovery::recovery_journal_path(&paths.manifest_dir),
        ] {
            let journal: recovery::RecoveryJournal =
                recovery::read_json(&journal_path, "recovery journal")
                    .expect("read recovery journal")
                    .expect("recovery journal");
            assert_eq!(
                journal.metadata_after_sha256, expected_metadata_hash,
                "each recovery journal must describe actual metadata after parent removal"
            );
        }
        assert!(profile_path(&paths, &first_id).exists());

        assert!(current_friend_key(&paths, "http://127.0.0.1:3000").is_err());
        assert!(restore(&paths, || Ok(())).is_err());

        fs::remove_file(&journal_path).expect("remove test journal");
        fs::remove_file(recovery::recovery_journal_path(&paths.manifest_dir))
            .expect("remove fallback test journal");
        assert!(current_friend_key(&paths, "http://127.0.0.1:3000").is_err());
        assert!(restore(&paths, || Ok(())).is_err());
        assert!(configure(
            &paths,
            &fixture_entry(),
            "v1a-test-1",
            "http://127.0.0.1:3000",
            "fake-key-after-old-failure",
            || Ok(()),
            || Ok(()),
            || Ok(()),
        )
        .is_err());
        let _ = fs::remove_dir_all(paths.library.parent().expect("parent"));
    }

    #[test]
    fn restore_rejects_missing_anchor_without_changing_metadata() {
        let paths = temp_paths();
        fs::create_dir_all(&paths.library).expect("library");
        let original_profile = paths.library.join("official-profile.json");
        fs::write(&original_profile, br#"{"provider":"official"}"#).expect("official profile");
        let original = json!({
            "appliedId": "official-profile",
            "entries": [{"id": "official-profile", "name": "Official profile"}]
        });
        recovery::write_json_atomic(&paths.metadata, &original).expect("metadata");
        configure(
            &paths,
            &fixture_entry(),
            "v1a-test-1",
            "http://127.0.0.1:3000",
            "fake-key-anchor",
            || Ok(()),
            || Ok(()),
            || Ok(()),
        )
        .expect("configure");
        fs::remove_file(&original_profile).expect("remove anchor fixture");
        let metadata_before = fs::read(&paths.metadata).expect("metadata before");

        let error = restore(&paths, || Ok(())).expect_err("missing anchor must block restore");
        assert!(error.starts_with("RECOVERY_REQUIRED:"));
        assert_eq!(
            fs::read(&paths.metadata).expect("metadata after"),
            metadata_before
        );
        assert!(!recovery::recovery_journal_exists(&paths.library).expect("journal check"));
        let _ = fs::remove_dir_all(paths.library.parent().expect("parent"));
    }

    #[test]
    fn restore_stop_failure_is_fail_closed_before_any_configuration_read_or_write() {
        let paths = temp_paths();
        fs::create_dir_all(&paths.library).expect("library");
        recovery::write_json_atomic(
            &paths.metadata,
            &json!({"appliedId": "", "entries": [], "keep": true}),
        )
        .expect("metadata");
        configure(
            &paths,
            &fixture_entry(),
            "v1a-test-1",
            "http://127.0.0.1:3000",
            "fixture-stop-failure",
            || Ok(()),
            || Ok(()),
            || Ok(()),
        )
        .expect("configure");
        let generation_id =
            applied_id(&read_metadata(&paths).expect("metadata").0).expect("generation id");
        let metadata_before = fs::read(&paths.metadata).expect("metadata bytes");
        let profile = profile_path(&paths, &generation_id);
        let profile_before = fs::read(&profile).expect("profile bytes");
        let manifest = manifest_path(&paths, &generation_id);
        let manifest_before = fs::read(&manifest).expect("manifest bytes");

        let error = restore(&paths, || Err("官方 Claude 仍在运行".into()))
            .expect_err("restore must reject when stop/recheck fails");
        assert!(error.contains("恢复前未能停止官方 App"));
        assert_eq!(
            fs::read(&paths.metadata).expect("metadata after"),
            metadata_before
        );
        assert_eq!(fs::read(&profile).expect("profile after"), profile_before);
        assert_eq!(
            fs::read(&manifest).expect("manifest after"),
            manifest_before
        );
        assert!(!recovery::recovery_journal_exists(&paths.library).expect("journal check"));
        let _ = fs::remove_dir_all(paths.library.parent().expect("parent"));
    }
}
