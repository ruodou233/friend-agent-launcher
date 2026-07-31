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
    env, fs,
    io::ErrorKind,
    path::{Path, PathBuf},
};

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
    previous_applied_id: Option<String>,
    catalog_version: &str,
    metadata_snapshot: &FileSnapshot,
) -> GenerationManifest {
    GenerationManifest {
        schema_version: MANIFEST_SCHEMA_VERSION,
        generation_id: generation_id.to_string(),
        parent_generation_id,
        previous_applied_id,
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

fn remove_owned_file(path: &Path, label: &str) -> Result<(), String> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(()),
        Err(_) => Err(recovery::recovery_required(&format!(
            "{label}删除结果不确定"
        ))),
    }
}

fn remove_new_generation(paths: &ClaudePaths, generation_id: &str) -> Result<(), String> {
    let profile = profile_path(paths, generation_id);
    let manifest = manifest_path(paths, generation_id);
    if profile.exists() {
        let proof = recovery::read_manifest(&manifest)?;
        recovery::validate_manifest_identity(&proof, generation_id, &profile, &manifest)?;
        let value: Value = recovery::read_json(&profile, "Friend 新 profile")?
            .ok_or_else(|| recovery::recovery_required("Friend 新 profile 消失"))?;
        if !recovery::profile_is_owned(&value, generation_id) {
            return Err(recovery::recovery_required(
                "新 profile 所有权不确定，停止删除",
            ));
        }
        if proof.profile_after_sha256.as_ref()
            != recovery::snapshot(&profile, "Friend 新 profile")?
                .sha256()
                .as_ref()
        {
            return Err(recovery::recovery_required(
                "新 profile 已被外部修改，停止删除",
            ));
        }
        remove_owned_file(&profile, "新 Friend profile")?;
    }
    remove_owned_file(&manifest, "新 generation manifest")
}

fn rollback_before_commit(
    paths: &ClaudePaths,
    generation_id: &str,
    metadata_before: &FileSnapshot,
    metadata_after: Option<&FileSnapshot>,
) -> Result<(), String> {
    let current = recovery::snapshot(&paths.metadata, "当前 Claude 元数据")?;
    if let Some(after) = metadata_after {
        if current.exists != after.exists || current.bytes != after.bytes {
            return Err(recovery::recovery_required("元数据在补偿前发生未知修改"));
        }
        recovery::restore_snapshot(&paths.metadata, metadata_before)?;
    } else if current.exists != metadata_before.exists || current.bytes != metadata_before.bytes {
        return Err(recovery::recovery_required("元数据在补偿前发生未知修改"));
    }
    remove_new_generation(paths, generation_id)
}

fn fail_before_commit(
    paths: &ClaudePaths,
    generation_id: &str,
    metadata_before: &FileSnapshot,
    metadata_after: Option<&FileSnapshot>,
    reason: &str,
) -> String {
    match rollback_before_commit(paths, generation_id, metadata_before, metadata_after) {
        Ok(()) => reason.into(),
        Err(recovery) => recovery,
    }
}

fn remove_parent_entry(
    paths: &ClaudePaths,
    generation_id: &str,
    expected_metadata: &FileSnapshot,
) -> Result<(), String> {
    let current = recovery::snapshot(&paths.metadata, "当前 Claude 元数据")?;
    if current.exists != expected_metadata.exists || current.bytes != expected_metadata.bytes {
        return Err(recovery::recovery_required(
            "提交后检测到用户修改，停止旧代清理",
        ));
    }
    let mut metadata: Value = serde_json::from_slice(&current.bytes)
        .map_err(|_| recovery::recovery_required("提交后元数据无法解析"))?;
    let entry = metadata_entry(&metadata, generation_id)
        .ok_or_else(|| recovery::recovery_required("旧 Friend 代条目缺失"))?;
    let _ = owned_manifest(paths, generation_id, entry)?;
    entries_mut(&mut metadata)?
        .retain(|entry| entry.get("id").and_then(Value::as_str) != Some(generation_id));
    recovery::write_json_atomic(&paths.metadata, &metadata)?;
    Ok(())
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
    // A legacy Keychain item is never deleted here: this V1A manifest does not
    // prove ownership of any pre-existing Keychain record.
    remove_owned_file(&profile_path, "旧 Friend profile")?;
    remove_owned_file(&manifest_path, "旧 generation manifest")
}

pub(crate) fn configure<F>(
    paths: &ClaudePaths,
    entry: &CatalogEntry,
    catalog_version: &str,
    gateway_url: &str,
    secret: &str,
    launch: F,
) -> Result<(), String>
where
    F: FnOnce() -> Result<(), String>,
{
    secure_store::validate_secret(secret)?;
    if gateway_url.trim().is_empty() {
        return Err(recovery::recovery_required("固定网关缺失"));
    }

    let (metadata, metadata_before) = read_metadata(paths)?;
    let previous_applied_id = applied_id(&metadata);
    let parent_generation_id = if let Some(current_id) = previous_applied_id.as_deref() {
        match metadata_entry(&metadata, current_id) {
            Some(entry) if recovery::metadata_entry_is_owned(entry, current_id) => {
                let parent = owned_manifest(paths, current_id, entry)?;
                if parent.commit_state != CommitState::Committed {
                    return Err(recovery::recovery_required("旧 Friend 代尚未提交"));
                }
                Some(current_id.to_string())
            }
            Some(_) => None,
            None => None,
        }
    } else {
        None
    };

    let generation_id = secure_store::new_opaque_id("generation")?;
    let profile = profile_path(paths, &generation_id);
    let manifest_file = manifest_path(paths, &generation_id);
    if profile.exists() || manifest_file.exists() {
        return Err(recovery::recovery_required("generation 标识冲突"));
    }
    let mut manifest = new_manifest(
        paths,
        &generation_id,
        parent_generation_id.clone(),
        previous_applied_id.clone(),
        catalog_version,
        &metadata_before,
    );
    recovery::write_manifest(&manifest_file, &manifest)?;

    manifest.phase = Phase::OwnershipCapture;
    recovery::write_manifest(&manifest_file, &manifest)?;
    manifest.phase = Phase::WriteNewGeneration;
    recovery::write_manifest(&manifest_file, &manifest)?;

    let new_profile = profile_for(entry, &generation_id, gateway_url, secret);
    recovery::write_json_atomic(&profile, &new_profile)?;
    let read_back_profile: Value = recovery::read_json(&profile, "Friend 新 profile")?
        .ok_or_else(|| recovery::recovery_required("Friend 新 profile 读回失败"))?;
    verify_profile(
        &read_back_profile,
        entry,
        &generation_id,
        gateway_url,
        secret,
    )?;
    manifest.profile_after_sha256 = recovery::snapshot(&profile, "Friend 新 profile")?.sha256();
    manifest.phase = Phase::ReadbackVerify;
    recovery::write_manifest(&manifest_file, &manifest)?;

    if !recovery::current_matches(&paths.metadata, &metadata_before)? {
        return Err(fail_before_commit(
            paths,
            &generation_id,
            &metadata_before,
            None,
            &recovery::recovery_required("元数据在切换前被修改"),
        ));
    }
    let mut switched_metadata = metadata.clone();
    entries_mut(&mut switched_metadata)?.push(generation_entry(paths, &generation_id));
    switched_metadata["appliedId"] = Value::String(generation_id.clone());
    recovery::write_json_atomic(&paths.metadata, &switched_metadata)?;
    let metadata_after = recovery::snapshot(&paths.metadata, "Claude 切换后元数据")?;
    manifest.metadata_after_sha256 = metadata_after.sha256();
    manifest.phase = Phase::MetadataSwitch;
    recovery::write_manifest(&manifest_file, &manifest)?;

    manifest.phase = Phase::OfficialAppVerify;
    recovery::write_manifest(&manifest_file, &manifest)?;
    if let Err(error) = launch() {
        return Err(fail_before_commit(
            paths,
            &generation_id,
            &metadata_before,
            Some(&metadata_after),
            &format!("官方 App 启动失败：{error}"),
        ));
    }

    manifest.phase = Phase::Commit;
    manifest.commit_state = CommitState::Committed;
    recovery::write_manifest(&manifest_file, &manifest).map_err(|_| {
        fail_before_commit(
            paths,
            &generation_id,
            &metadata_before,
            Some(&metadata_after),
            &recovery::recovery_required("COMMIT 记录失败"),
        )
    })?;

    if let Some(parent_id) = parent_generation_id {
        manifest.phase = Phase::DeleteOldFriendGeneration;
        recovery::write_manifest(&manifest_file, &manifest)?;
        if let Err(error) = remove_parent_entry(paths, &parent_id, &metadata_after)
            .and_then(|_| delete_old_generation(paths, &parent_id))
        {
            manifest.delete_state = DeleteState::RecoveryRequired;
            let _ = recovery::write_manifest(&manifest_file, &manifest);
            return Err(error);
        }
    }
    manifest.delete_state = DeleteState::Deleted;
    recovery::write_manifest(&manifest_file, &manifest)?;
    Ok(())
}

pub(crate) fn current_friend_key(paths: &ClaudePaths, gateway_url: &str) -> Result<String, String> {
    let (metadata, _) = read_metadata(paths)?;
    let current_id =
        applied_id(&metadata).ok_or_else(|| "未找到 Friend 自有 Claude profile".to_string())?;
    let entry = metadata_entry(&metadata, &current_id)
        .ok_or_else(|| "未找到 Friend 自有 Claude profile".to_string())?;
    let manifest = owned_manifest(paths, &current_id, entry)?;
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

pub(crate) fn restore(paths: &ClaudePaths) -> Result<bool, String> {
    let (metadata, _) = read_metadata(paths)?;
    let current_id = match applied_id(&metadata) {
        Some(id) => id,
        None => return Ok(false),
    };
    let entry = match metadata_entry(&metadata, &current_id) {
        Some(entry) if recovery::metadata_entry_is_owned(entry, &current_id) => entry,
        Some(_) | None => return Ok(false),
    };
    let manifest = owned_manifest(paths, &current_id, entry)?;
    if manifest.commit_state != CommitState::Committed {
        return Err(recovery::recovery_required("待提交 Friend 代不能自动恢复"));
    }

    if let Some(previous_id) = manifest.previous_applied_id.as_deref() {
        if let Some(previous_entry) = metadata_entry(&metadata, previous_id) {
            if recovery::metadata_entry_is_owned(previous_entry, previous_id) {
                let _ = owned_manifest(paths, previous_id, previous_entry)
                    .map_err(|_| recovery::recovery_required("恢复目标 Friend 代所有权不确定"))?;
            }
        }
    }

    let profile = profile_path(paths, &current_id);
    let profile_value: Value = recovery::read_json(&profile, "当前 Friend profile")?
        .ok_or_else(|| recovery::recovery_required("当前 Friend profile 缺失"))?;
    if !recovery::profile_is_owned(&profile_value, &current_id) {
        return Err(recovery::recovery_required(
            "恢复前发现 Friend profile 所有权变化",
        ));
    }

    let mut restored = metadata.clone();
    entries_mut(&mut restored)?
        .retain(|item| item.get("id").and_then(Value::as_str) != Some(current_id.as_str()));
    restored["appliedId"] = Value::String(manifest.previous_applied_id.unwrap_or_default());
    recovery::write_json_atomic(&paths.metadata, &restored)?;

    remove_owned_file(&profile, "当前 Friend profile")?;
    remove_owned_file(
        &manifest_path(paths, &current_id),
        "当前 generation manifest",
    )?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gateway::{CatalogEntry, FIXED_GATEWAY_REF, PRODUCT, PROTOCOL};

    fn fixture_entry() -> CatalogEntry {
        CatalogEntry {
            product: PRODUCT.into(),
            protocol: PROTOCOL.into(),
            canonical_id: "claude.default".into(),
            model_ref: "anthropic/claude-test".into(),
            gateway_ref: FIXED_GATEWAY_REF.into(),
            display_name: "Claude 默认".into(),
            capabilities: vec!["streaming".into(), "tool_use".into()],
            default: true,
            catalog_version: "v1a-test-1".into(),
            expires_at: "2099-01-01T00:00:00Z".into(),
            billing_label: "按量计费".into(),
        }
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

        assert!(restore(&paths).expect("restore"));
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
        )
        .expect("configure");
        let (metadata, _) = read_metadata(&paths).expect("read metadata");
        let generation_id = applied_id(&metadata).expect("friend id");
        let profile = profile_path(&paths, &generation_id);
        let mut profile_value: Value = recovery::read_json(&profile, "profile")
            .expect("read")
            .expect("profile");
        profile_value["disableDeploymentModeChooser"] = Value::Bool(false);
        recovery::write_json_atomic(&profile, &profile_value).expect("mutate fixture");

        let balance_error = current_friend_key(&paths, "http://127.0.0.1:3000")
            .expect_err("changed profile must not be read");
        assert!(balance_error.starts_with("RECOVERY_REQUIRED:"));
        assert!(restore(&paths).is_err());
        let (still_current, _) = read_metadata(&paths).expect("metadata remains");
        assert_eq!(still_current["appliedId"], generation_id);
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
            || Err("process not started".into()),
        );
        assert!(result.is_err());
        let (after, _) = read_metadata(&paths).expect("after");
        assert_eq!(after, before);
        let _ = fs::remove_dir_all(paths.library.parent().expect("parent"));
    }
}
