use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    env, fs,
    io::Write,
    path::{Path, PathBuf},
    process::Command,
    thread,
    time::Duration,
};
use tauri::{AppHandle, Manager};
use toml_edit::{value, DocumentMut, Item, Table};

const KEYRING_SERVICE: &str = "cn.ruodou.friend-agent-launcher";
const CLAUDE_PROFILE_ID: &str = "6a9434b2-9ee5-4aa5-99a1-ae6feab0da84";
const CODEX_PROVIDER_ID: &str = "friend_gateway";
const MAX_ERROR_BYTES: usize = 16 * 1024;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Product {
    Claude,
    Codex,
}

impl Product {
    fn account(self) -> &'static str {
        match self {
            Self::Claude => "claude",
            Self::Codex => "codex",
        }
    }
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct SavedSettings {
    endpoint: String,
    model: String,
}

#[derive(Debug, Serialize)]
struct LauncherStatus {
    official_app_installed: bool,
    official_app_version: Option<String>,
    has_saved_secret: bool,
    endpoint: String,
    model: String,
}

#[derive(Debug, Deserialize)]
struct ConfigureRequest {
    endpoint: String,
    model: String,
    secret: String,
}

#[derive(Debug, Deserialize)]
struct ModelRequest {
    endpoint: String,
    secret: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct ClaudeRestore {
    previous_applied_id: Option<String>,
    previous_profile: Option<Value>,
}

#[derive(Debug, Serialize, Deserialize)]
struct CodexRestore {
    model: Option<String>,
    model_provider: Option<String>,
    previous_friend_provider: Option<String>,
}

fn product(app: &AppHandle) -> Product {
    if app.config().identifier.contains("claude") {
        Product::Claude
    } else {
        Product::Codex
    }
}

fn product_from_executable() -> Product {
    let executable = env::current_exe()
        .ok()
        .and_then(|path| {
            path.file_name()
                .map(|name| name.to_string_lossy().to_lowercase())
        })
        .unwrap_or_default();
    if executable.contains("claude") {
        Product::Claude
    } else {
        Product::Codex
    }
}

fn home_dir() -> Result<PathBuf, String> {
    env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
        .map(PathBuf::from)
        .ok_or_else(|| "无法确定用户目录".to_string())
}

fn local_app_data() -> Result<PathBuf, String> {
    if cfg!(windows) {
        env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .ok_or_else(|| "无法确定 Windows LocalAppData".to_string())
    } else {
        Ok(home_dir()?.join("Library/Application Support"))
    }
}

fn app_data_dir(app: &AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_data_dir()
        .map_err(|error| format!("无法确定启动器数据目录：{error}"))
}

fn settings_path(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(app_data_dir(app)?.join("settings.json"))
}

fn restore_path(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(app_data_dir(app)?.join("restore.json"))
}

fn keyring_entry(product: Product) -> Result<keyring::Entry, String> {
    keyring::Entry::new(KEYRING_SERVICE, product.account())
        .map_err(|error| format!("无法打开系统凭据库：{error}"))
}

fn get_secret(product: Product) -> Result<String, String> {
    keyring_entry(product)?
        .get_password()
        .map_err(|_| "还没有保存 Key，请先粘贴 Key".to_string())
}

fn has_secret(product: Product) -> bool {
    get_secret(product)
        .map(|secret| !secret.trim().is_empty())
        .unwrap_or(false)
}

fn validate_secret(secret: &str) -> Result<(), String> {
    if secret.trim().is_empty() || secret.len() > 20_000 || secret.contains(['\r', '\n', '\0']) {
        return Err("Key 格式无效".into());
    }
    Ok(())
}

fn validate_endpoint(endpoint: &str) -> Result<(), String> {
    let endpoint = endpoint.trim();
    let parsed = reqwest::Url::parse(endpoint).map_err(|_| "API 地址格式无效".to_string())?;
    let local_debug = cfg!(debug_assertions)
        && parsed.scheme() == "http"
        && matches!(parsed.host_str(), Some("127.0.0.1" | "localhost"));
    if parsed.scheme() != "https" && !local_debug {
        return Err("API 地址必须使用 HTTPS".into());
    }
    if parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || endpoint.len() > 500
        || endpoint.contains(['\r', '\n', '\t', ' '])
    {
        return Err("API 地址格式无效".into());
    }
    Ok(())
}

fn validate_model(model: &str) -> Result<(), String> {
    if model.trim().is_empty() || model.len() > 200 || model.contains(['\r', '\n', '\t', ' ']) {
        return Err("模型名称格式无效".into());
    }
    Ok(())
}

fn write_json_atomic(path: &Path, value: &impl Serialize) -> Result<(), String> {
    let data =
        serde_json::to_vec_pretty(value).map_err(|error| format!("编码 JSON 失败：{error}"))?;
    write_bytes_transactional(path, &data)
}

fn write_text_atomic(path: &Path, text: &str) -> Result<(), String> {
    write_bytes_transactional(path, text.as_bytes())
}

fn write_bytes_transactional(path: &Path, data: &[u8]) -> Result<(), String> {
    let parent = path.parent().ok_or("目标目录无效")?;
    fs::create_dir_all(parent).map_err(|error| format!("创建目录失败：{error}"))?;
    let temporary = path.with_extension("friend-agent.tmp");
    let backup = path.with_extension("friend-agent.bak");
    if backup.exists() {
        if path.exists() {
            fs::remove_file(&backup)
                .map_err(|error| format!("清理上次已完成写入的备份失败：{error}"))?;
        } else {
            fs::rename(&backup, path)
                .map_err(|error| format!("恢复上次中断的配置失败：{error}"))?;
        }
    }
    let _ = fs::remove_file(&temporary);
    fs::write(&temporary, data).map_err(|error| format!("写入临时文件失败：{error}"))?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = fs::metadata(path)
            .map(|metadata| metadata.permissions().mode())
            .unwrap_or(0o600);
        fs::set_permissions(&temporary, fs::Permissions::from_mode(mode))
            .map_err(|error| format!("设置配置权限失败：{error}"))?;
    }

    if path.exists() {
        fs::rename(path, &backup).map_err(|error| format!("备份现有配置失败：{error}"))?;
    }
    if let Err(error) = fs::rename(&temporary, path) {
        if backup.exists() {
            let _ = fs::rename(&backup, path);
        }
        return Err(format!("提交配置失败：{error}"));
    }
    if backup.exists() {
        let _ = fs::remove_file(&backup);
    }
    Ok(())
}

fn read_optional_bytes(path: &Path, label: &str) -> Result<Option<Vec<u8>>, String> {
    match fs::read(path) {
        Ok(data) => Ok(Some(data)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!("读取{label}失败：{error}")),
    }
}

fn read_optional_text(path: &Path, label: &str) -> Result<Option<String>, String> {
    match fs::read_to_string(path) {
        Ok(text) => Ok(Some(text)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!("读取{label}失败：{error}")),
    }
}

fn read_optional_json(path: &Path, label: &str) -> Result<Option<Value>, String> {
    read_optional_bytes(path, label)?
        .map(|data| {
            serde_json::from_slice(&data).map_err(|error| format!("{label} JSON 已损坏：{error}"))
        })
        .transpose()
}

fn read_saved_settings(app: &AppHandle) -> SavedSettings {
    settings_path(app)
        .ok()
        .and_then(|path| fs::read(path).ok())
        .and_then(|data| serde_json::from_slice(&data).ok())
        .unwrap_or_default()
}

fn save_settings(app: &AppHandle, endpoint: &str, model: &str) -> Result<(), String> {
    write_json_atomic(
        &settings_path(app)?,
        &SavedSettings {
            endpoint: endpoint.into(),
            model: model.into(),
        },
    )
}

fn official_app_path(product: Product) -> Option<PathBuf> {
    #[cfg(target_os = "macos")]
    {
        let name = match product {
            Product::Claude => "Claude.app",
            Product::Codex => "ChatGPT.app",
        };
        for root in [
            PathBuf::from("/Applications"),
            home_dir().ok()?.join("Applications"),
        ] {
            let candidate = root.join(name);
            if candidate.is_dir() {
                return Some(candidate);
            }
        }
    }
    #[cfg(windows)]
    {
        let local = local_app_data().ok()?;
        let candidates = match product {
            Product::Claude => vec![
                local.join("Programs/Claude/Claude.exe"),
                local.join("AnthropicClaude/Claude.exe"),
            ],
            Product::Codex => vec![
                local.join("Programs/OpenAI/ChatGPT/ChatGPT.exe"),
                local.join("Programs/OpenAI/Codex/Codex.exe"),
            ],
        };
        return candidates.into_iter().find(|path| path.is_file());
    }
    None
}

fn official_app_installed(product: Product) -> bool {
    if official_app_path(product).is_some() {
        return true;
    }
    #[cfg(windows)]
    {
        let scheme = match product {
            Product::Claude => "claude",
            Product::Codex => "codex",
        };
        for root in ["HKCU\\Software\\Classes", "HKCR"] {
            let key = format!("{root}\\{scheme}");
            if Command::new("reg.exe")
                .args(["query", &key])
                .output()
                .map(|output| output.status.success())
                .unwrap_or(false)
            {
                return true;
            }
        }
    }
    false
}

#[tauri::command]
fn launcher_status(app: AppHandle) -> Result<LauncherStatus, String> {
    let product = product(&app);
    let settings = read_saved_settings(&app);
    Ok(LauncherStatus {
        official_app_installed: official_app_installed(product),
        official_app_version: None,
        has_saved_secret: has_secret(product),
        endpoint: settings.endpoint,
        model: settings.model,
    })
}

fn api_url(endpoint: &str, path: &str) -> String {
    let base = endpoint.trim_end_matches('/');
    if base.ends_with("/v1") {
        format!("{base}/{path}")
    } else {
        format!("{base}/v1/{path}")
    }
}

fn api_base_url(endpoint: &str) -> String {
    let base = endpoint.trim_end_matches('/');
    if base.ends_with("/v1") {
        base.to_string()
    } else {
        format!("{base}/v1")
    }
}

fn limited_error(response: reqwest::blocking::Response, secret: &str) -> String {
    let status = response.status();
    let mut text = response.text().unwrap_or_default();
    if text.len() > MAX_ERROR_BYTES {
        text.truncate(MAX_ERROR_BYTES);
    }
    if !secret.is_empty() {
        text = text.replace(secret, "[已隐藏 Key]");
    }
    format!("HTTP {}：{}", status.as_u16(), text.trim())
}

fn test_gateway(product: Product, endpoint: &str, model: &str, secret: &str) -> Result<(), String> {
    let client = reqwest::blocking::Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(45))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|error| format!("初始化连接测试失败：{error}"))?;

    let models = client
        .get(api_url(endpoint, "models"))
        .bearer_auth(secret)
        .send()
        .map_err(|error| format!("无法连接模型目录：{error}"))?;
    if !models.status().is_success()
        && models.status() != reqwest::StatusCode::NOT_FOUND
        && models.status() != reqwest::StatusCode::METHOD_NOT_ALLOWED
    {
        return Err(format!(
            "Key 或 API 地址不可用：{}",
            limited_error(models, secret)
        ));
    }

    let response = match product {
        Product::Claude => client
            .post(api_url(endpoint, "messages"))
            .bearer_auth(secret)
            .header("anthropic-version", "2023-06-01")
            .json(&json!({
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "Reply OK"}]
            }))
            .send(),
        Product::Codex => client
            .post(api_url(endpoint, "responses"))
            .bearer_auth(secret)
            .json(&json!({
                "model": model,
                "input": "Reply OK",
                "max_output_tokens": 1,
                "stream": false
            }))
            .send(),
    }
    .map_err(|error| format!("最小模型调用失败：{error}"))?;

    if response.status().is_success() {
        Ok(())
    } else {
        Err(format!(
            "模型或协议不可用：{}",
            limited_error(response, secret)
        ))
    }
}

#[tauri::command]
fn discover_models(app: AppHandle, request: ModelRequest) -> Result<Vec<String>, String> {
    let endpoint = request.endpoint.trim().trim_end_matches('/');
    validate_endpoint(endpoint)?;
    let product = product(&app);
    let secret = if request.secret.trim().is_empty() {
        get_secret(product)?
    } else {
        validate_secret(&request.secret)?;
        request.secret.trim().to_string()
    };
    let client = reqwest::blocking::Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|error| format!("初始化模型列表失败：{error}"))?;
    let response = client
        .get(api_url(endpoint, "models"))
        .bearer_auth(&secret)
        .send()
        .map_err(|error| format!("获取模型列表失败：{error}"))?;
    if !response.status().is_success() {
        return Err(format!(
            "获取模型列表失败：{}",
            limited_error(response, &secret)
        ));
    }
    let body: Value = response
        .json()
        .map_err(|error| format!("模型列表格式无效：{error}"))?;
    let models = body
        .get("data")
        .and_then(Value::as_array)
        .ok_or("模型列表缺少 data 数组")?;
    let unique: BTreeSet<String> = models
        .iter()
        .filter_map(|item| item.get("id").and_then(Value::as_str))
        .map(str::trim)
        .filter(|id| !id.is_empty())
        .map(str::to_string)
        .collect();
    if unique.is_empty() {
        return Err("模型列表为空".into());
    }
    Ok(unique.into_iter().collect())
}

fn claude_library_dir() -> Result<PathBuf, String> {
    Ok(local_app_data()?.join("Claude-3p/configLibrary"))
}

fn apply_claude_meta(mut meta: Value) -> Result<(Value, Option<String>), String> {
    let previous_applied_id = meta
        .get("appliedId")
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|value| !value.is_empty());
    let entries = meta
        .get_mut("entries")
        .and_then(Value::as_array_mut)
        .ok_or("Claude 配置库元数据格式不兼容")?;
    entries.retain(|entry| entry.get("id").and_then(Value::as_str) != Some(CLAUDE_PROFILE_ID));
    entries.push(json!({"id": CLAUDE_PROFILE_ID, "name": "Friend Gateway"}));
    meta["appliedId"] = Value::String(CLAUDE_PROFILE_ID.into());
    Ok((meta, previous_applied_id))
}

fn claude_model_entry(model: &str) -> Value {
    let lower = model.to_lowercase();
    let tier = ["haiku", "sonnet", "opus", "fable", "mythos"]
        .into_iter()
        .find(|tier| lower.contains(tier));
    match tier {
        Some(tier) => {
            json!({"name": model, "anthropicFamilyTier": tier, "isFamilyDefault": true})
        }
        None => json!({"name": model}),
    }
}

fn claude_profile(endpoint: &str, model: &str, secret: &str) -> Value {
    json!({
        "inferenceProvider": "gateway",
        "inferenceCredentialKind": "static",
        "inferenceGatewayBaseUrl": endpoint,
        "inferenceGatewayApiKey": secret,
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceModels": [claude_model_entry(model)],
        "disableDeploymentModeChooser": true
    })
}

fn configure_claude(
    app: &AppHandle,
    endpoint: &str,
    model: &str,
    secret: &str,
) -> Result<(), String> {
    let library = claude_library_dir()?;
    fs::create_dir_all(&library)
        .map_err(|error| format!("创建 Claude 3P 配置目录失败：{error}"))?;
    let meta_path = library.join("_meta.json");
    let profile_path = library.join(format!("{CLAUDE_PROFILE_ID}.json"));

    if !restore_path(app)?.exists() {
        let meta = read_optional_json(&meta_path, "Claude 配置库元数据")?
            .unwrap_or_else(|| json!({"appliedId": "", "entries": []}));
        let previous_applied_id = meta
            .get("appliedId")
            .and_then(Value::as_str)
            .map(str::to_string)
            .filter(|value| !value.is_empty());
        let previous_profile = read_optional_json(&profile_path, "Claude 原有朋友线路配置")?;
        write_json_atomic(
            &restore_path(app)?,
            &ClaudeRestore {
                previous_applied_id,
                previous_profile,
            },
        )?;
    }

    let mut meta = read_optional_json(&meta_path, "Claude 配置库元数据")?
        .unwrap_or_else(|| json!({"appliedId": "", "entries": []}));
    (meta, _) = apply_claude_meta(meta)?;
    let profile = claude_profile(endpoint, model, secret);
    write_json_atomic(&profile_path, &profile)?;
    if let Err(error) = write_json_atomic(&meta_path, &meta) {
        let _ = restore_claude(app);
        return Err(error);
    }
    Ok(())
}

fn codex_config_path() -> Result<PathBuf, String> {
    Ok(home_dir()?.join(".codex/config.toml"))
}

fn codex_credential_helper_path(app: &AppHandle) -> Result<PathBuf, String> {
    let filename = if cfg!(windows) {
        "friend-codex-credential-helper.exe"
    } else {
        "friend-codex-credential-helper"
    };
    Ok(app_data_dir(app)?.join("helpers").join(filename))
}

fn install_codex_credential_helper(app: &AppHandle) -> Result<PathBuf, String> {
    let destination = codex_credential_helper_path(app)?;
    let executable =
        env::current_exe().map_err(|error| format!("无法确定当前启动器路径：{error}"))?;
    let bytes = fs::read(&executable).map_err(|error| format!("读取凭据 Helper 失败：{error}"))?;
    write_bytes_transactional(&destination, &bytes)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&destination, fs::Permissions::from_mode(0o700))
            .map_err(|error| format!("设置凭据 Helper 权限失败：{error}"))?;
    }
    Ok(destination)
}

fn string_value(document: &DocumentMut, key: &str) -> Option<String> {
    document.get(key)?.as_str().map(str::to_string)
}

fn apply_codex_config(document: &mut DocumentMut, endpoint: &str, model: &str, helper_path: &Path) {
    document["model"] = value(model);
    document["model_provider"] = value(CODEX_PROVIDER_ID);
    if !document["model_providers"].is_table_like() {
        document["model_providers"] = Item::Table(Table::new());
    }
    document["model_providers"][CODEX_PROVIDER_ID]["name"] = value("Friend Gateway");
    document["model_providers"][CODEX_PROVIDER_ID]["base_url"] = value(api_base_url(endpoint));
    document["model_providers"][CODEX_PROVIDER_ID]["wire_api"] = value("responses");
    document["model_providers"][CODEX_PROVIDER_ID]["auth"]["command"] =
        value(helper_path.to_string_lossy().to_string());
    let mut args = toml_edit::Array::new();
    args.push("--credential-helper");
    args.push("codex");
    document["model_providers"][CODEX_PROVIDER_ID]["auth"]["args"] = value(args);
    document["model_providers"][CODEX_PROVIDER_ID]["auth"]["timeout_ms"] = value(5000);
}

fn restore_codex_document(document: &mut DocumentMut, restore: CodexRestore) -> Result<(), String> {
    let selected_ours =
        document.get("model_provider").and_then(Item::as_str) == Some(CODEX_PROVIDER_ID);
    if selected_ours {
        match restore.model {
            Some(model) => document["model"] = value(model),
            None => {
                document.remove("model");
            }
        }
        match restore.model_provider {
            Some(provider) => document["model_provider"] = value(provider),
            None => {
                document.remove("model_provider");
            }
        }
    }
    let provider_is_ours = document
        .get("model_providers")
        .and_then(Item::as_table_like)
        .and_then(|providers| providers.get(CODEX_PROVIDER_ID))
        .and_then(Item::as_table_like)
        .and_then(|provider| provider.get("name"))
        .and_then(Item::as_str)
        == Some("Friend Gateway");
    if provider_is_ours {
        if let Some(providers) = document
            .get_mut("model_providers")
            .and_then(Item::as_table_like_mut)
        {
            providers.remove(CODEX_PROVIDER_ID);
        }
        if let Some(previous) = restore.previous_friend_provider {
            let item = format!("[model_providers.{CODEX_PROVIDER_ID}]\n{previous}")
                .parse::<DocumentMut>()
                .map_err(|error| format!("旧 Provider 备份无法解析：{error}"))
                .and_then(|mut old| {
                    old["model_providers"]
                        .as_table_like_mut()
                        .and_then(|providers| providers.remove(CODEX_PROVIDER_ID))
                        .ok_or_else(|| "旧 Provider 备份格式不兼容".to_string())
                })?;
            document["model_providers"][CODEX_PROVIDER_ID] = item;
        }
    }
    Ok(())
}

fn configure_codex(app: &AppHandle, endpoint: &str, model: &str) -> Result<(), String> {
    let path = codex_config_path()?;
    let original = read_optional_text(&path, "Codex config.toml")?.unwrap_or_default();
    let mut document = if original.trim().is_empty() {
        DocumentMut::new()
    } else {
        original
            .parse::<DocumentMut>()
            .map_err(|error| format!("现有 Codex config.toml 无法解析：{error}"))?
    };

    if !restore_path(app)?.exists() {
        let previous_friend_provider = document
            .get("model_providers")
            .and_then(Item::as_table_like)
            .and_then(|providers| providers.get(CODEX_PROVIDER_ID))
            .map(ToString::to_string);
        write_json_atomic(
            &restore_path(app)?,
            &CodexRestore {
                model: string_value(&document, "model"),
                model_provider: string_value(&document, "model_provider"),
                previous_friend_provider,
            },
        )?;
    }

    let helper_path = install_codex_credential_helper(app)?;
    apply_codex_config(&mut document, endpoint, model, &helper_path);

    write_text_atomic(&path, &document.to_string())
}

fn launch_official(product: Product) -> Result<(), String> {
    if !official_app_installed(product) {
        return Err("尚未安装原版 App，请先点击“下载原版 App”".into());
    }
    close_official_if_running(product)?;
    #[cfg(target_os = "macos")]
    {
        let bundle_id = if product == Product::Claude {
            "com.anthropic.claudefordesktop"
        } else {
            "com.openai.codex"
        };
        Command::new("/usr/bin/open")
            .args(["-b", bundle_id])
            .spawn()
            .map_err(|error| format!("打开原版 App 失败：{error}"))?;
        return Ok(());
    }
    #[cfg(windows)]
    {
        let uri = if product == Product::Claude {
            "claude://"
        } else {
            "codex://"
        };
        Command::new("explorer.exe")
            .arg(uri)
            .spawn()
            .map_err(|error| format!("打开原版 App 失败：{error}"))?;
        return Ok(());
    }
    #[allow(unreachable_code)]
    Err("当前系统暂不支持自动打开官方 App".into())
}

fn official_process_running(product: Product) -> bool {
    #[cfg(target_os = "macos")]
    {
        let pattern = if product == Product::Claude {
            "/Claude.app/Contents/MacOS/Claude"
        } else {
            "/ChatGPT.app/Contents/MacOS/ChatGPT"
        };
        return Command::new("/usr/bin/pgrep")
            .args(["-f", pattern])
            .output()
            .map(|output| output.status.success())
            .unwrap_or(false);
    }
    #[cfg(windows)]
    {
        let names: &[&str] = if product == Product::Claude {
            &["claude.exe"]
        } else {
            &["chatgpt.exe", "codex.exe"]
        };
        let output = Command::new("tasklist")
            .args(["/NH", "/FO", "CSV"])
            .output()
            .ok();
        let text = output
            .map(|output| String::from_utf8_lossy(&output.stdout).to_lowercase())
            .unwrap_or_default();
        return names.iter().any(|name| text.contains(name));
    }
    #[allow(unreachable_code)]
    false
}

fn close_official_if_running(product: Product) -> Result<(), String> {
    if !official_process_running(product) {
        return Ok(());
    }
    #[cfg(target_os = "macos")]
    {
        let bundle_id = if product == Product::Claude {
            "com.anthropic.claudefordesktop"
        } else {
            "com.openai.codex"
        };
        let script = format!("tell application id \"{bundle_id}\" to quit");
        let _ = Command::new("/usr/bin/osascript")
            .args(["-e", &script])
            .status();
    }
    #[cfg(windows)]
    {
        let process_names = if product == Product::Claude {
            "@('Claude')"
        } else {
            "@('ChatGPT','Codex')"
        };
        let script = format!(
            "$names={process_names}; Get-Process -Name $names -ErrorAction SilentlyContinue | \
             ForEach-Object {{ $_.CloseMainWindow() | Out-Null }}"
        );
        let _ = Command::new("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", &script])
            .status();
    }
    for _ in 0..40 {
        if !official_process_running(product) {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(200));
    }
    Err("原版 App 正在运行且未能自动退出。请保存当前工作、完全退出原版 App，再点一次“开始使用”。".into())
}

#[tauri::command]
fn configure_and_launch(app: AppHandle, request: ConfigureRequest) -> Result<(), String> {
    let endpoint = request.endpoint.trim().trim_end_matches('/');
    let model = request.model.trim();
    validate_endpoint(endpoint)?;
    validate_model(model)?;
    let product = product(&app);
    let secret = if request.secret.trim().is_empty() {
        get_secret(product)?
    } else {
        validate_secret(&request.secret)?;
        request.secret.trim().to_string()
    };

    test_gateway(product, endpoint, model, &secret)?;
    keyring_entry(product)?
        .set_password(&secret)
        .map_err(|error| format!("保存 Key 到系统凭据库失败：{error}"))?;
    save_settings(&app, endpoint, model)?;
    match product {
        Product::Claude => configure_claude(&app, endpoint, model, &secret)?,
        Product::Codex => configure_codex(&app, endpoint, model)?,
    }
    launch_official(product)
}

fn restore_claude(app: &AppHandle) -> Result<bool, String> {
    let path = restore_path(app)?;
    if !path.exists() {
        return Ok(false);
    }
    let restore: ClaudeRestore = serde_json::from_slice(
        &fs::read(&path).map_err(|error| format!("读取恢复信息失败：{error}"))?,
    )
    .map_err(|error| format!("恢复信息损坏：{error}"))?;
    let library = claude_library_dir()?;
    let meta_path = library.join("_meta.json");
    let profile_path = library.join(format!("{CLAUDE_PROFILE_ID}.json"));
    let mut meta = read_optional_json(&meta_path, "Claude 配置库元数据")?
        .unwrap_or_else(|| json!({"appliedId": "", "entries": []}));
    if let Some(entries) = meta.get_mut("entries").and_then(Value::as_array_mut) {
        entries.retain(|entry| entry.get("id").and_then(Value::as_str) != Some(CLAUDE_PROFILE_ID));
    }
    if meta.get("appliedId").and_then(Value::as_str) == Some(CLAUDE_PROFILE_ID) {
        meta["appliedId"] = restore
            .previous_applied_id
            .map(Value::String)
            .unwrap_or_else(|| Value::String(String::new()));
    }
    write_json_atomic(&meta_path, &meta)?;
    match restore.previous_profile {
        Some(profile) => write_json_atomic(&profile_path, &profile)?,
        None => {
            let _ = fs::remove_file(&profile_path);
        }
    }
    fs::remove_file(path).map_err(|error| format!("清理恢复信息失败：{error}"))?;
    Ok(true)
}

fn restore_codex(app: &AppHandle) -> Result<bool, String> {
    let restore_file = restore_path(app)?;
    if !restore_file.exists() {
        return Ok(false);
    }
    let restore: CodexRestore = serde_json::from_slice(
        &fs::read(&restore_file).map_err(|error| format!("读取恢复信息失败：{error}"))?,
    )
    .map_err(|error| format!("恢复信息损坏：{error}"))?;
    let path = codex_config_path()?;
    let current = read_optional_text(&path, "当前 Codex config.toml")?.unwrap_or_default();
    let mut document = current
        .parse::<DocumentMut>()
        .map_err(|error| format!("当前 Codex config.toml 无法解析：{error}"))?;

    restore_codex_document(&mut document, restore)?;
    write_text_atomic(&path, &document.to_string())?;
    fs::remove_file(restore_file).map_err(|error| format!("清理恢复信息失败：{error}"))?;
    Ok(true)
}

#[tauri::command]
fn restore_official_mode(app: AppHandle) -> Result<bool, String> {
    let product = product(&app);
    let restored = match product {
        Product::Claude => restore_claude(&app),
        Product::Codex => restore_codex(&app),
    }?;
    if restored {
        let _ = keyring_entry(product).and_then(|entry| {
            entry
                .delete_credential()
                .map_err(|error| format!("清理系统凭据失败：{error}"))
        });
        if let Ok(path) = settings_path(&app) {
            let _ = fs::remove_file(path);
        }
        if product == Product::Codex {
            if let Ok(path) = codex_credential_helper_path(&app) {
                let _ = fs::remove_file(path);
            }
        }
    }
    Ok(restored)
}

#[tauri::command]
fn open_allowed_link(app: AppHandle, target: String) -> Result<(), String> {
    let url = match target.as_str() {
        "official-download" => match product(&app) {
            Product::Claude => "https://claude.com/download",
            Product::Codex => "https://chatgpt.com/download/",
        },
        "free-token" => "https://github.com/ruodou233/free-token-eggs",
        "invite" => "https://github.com/ruodou233/friend-agent-launcher/releases",
        "our-gateway" => "https://github.com/ruodou233/friend-agent-launcher#获取-key",
        _ => return Err("不允许打开这个地址".into()),
    };
    open::that(url).map_err(|error| format!("打开系统浏览器失败：{error}"))
}

pub fn run_credential_helper_if_requested() -> bool {
    let arguments: Vec<String> = env::args().collect();
    if arguments.get(1).map(String::as_str) != Some("--credential-helper") {
        return false;
    }
    let requested = arguments.get(2).map(String::as_str).unwrap_or_default();
    let product = match requested {
        "claude" => Product::Claude,
        "codex" => Product::Codex,
        _ => product_from_executable(),
    };
    match get_secret(product) {
        Ok(secret) => {
            let _ = std::io::stdout().write_all(secret.as_bytes());
            std::process::exit(0);
        }
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(1);
        }
    }
}

pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            launcher_status,
            discover_models,
            configure_and_launch,
            restore_official_mode,
            open_allowed_link
        ])
        .run(tauri::generate_context!())
        .expect("Friend Agent Launcher 启动失败");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn api_url_handles_versioned_and_unversioned_bases() {
        assert_eq!(
            api_url("https://gateway.example.com", "responses"),
            "https://gateway.example.com/v1/responses"
        );
        assert_eq!(
            api_url("https://gateway.example.com/v1/", "models"),
            "https://gateway.example.com/v1/models"
        );
        assert_eq!(
            api_base_url("https://gateway.example.com"),
            "https://gateway.example.com/v1"
        );
        assert_eq!(
            api_base_url("https://gateway.example.com/v1/"),
            "https://gateway.example.com/v1"
        );
    }

    #[test]
    fn release_endpoints_must_use_https() {
        assert!(validate_endpoint("https://gateway.example.com").is_ok());
        if !cfg!(debug_assertions) {
            assert!(validate_endpoint("http://127.0.0.1:3000").is_err());
        }
        assert!(validate_endpoint("http://example.com").is_err());
    }

    #[test]
    fn model_validation_rejects_shell_or_line_breaks() {
        assert!(validate_model("gpt-5.4").is_ok());
        assert!(validate_model("bad model").is_err());
        assert!(validate_model("bad\nmodel").is_err());
    }

    #[test]
    fn claude_meta_keeps_other_profiles_and_selects_friend_profile() {
        let original = json!({
            "appliedId": "existing-profile",
            "entries": [
                {"id": "existing-profile", "name": "Existing"},
                {"id": CLAUDE_PROFILE_ID, "name": "Old friend entry"}
            ],
            "unknown": {"keep": true}
        });
        let (updated, previous) = apply_claude_meta(original).expect("Claude meta should update");
        assert_eq!(previous.as_deref(), Some("existing-profile"));
        assert_eq!(updated["appliedId"], CLAUDE_PROFILE_ID);
        assert_eq!(updated["unknown"]["keep"], true);
        let friend_entries = updated["entries"]
            .as_array()
            .expect("entries")
            .iter()
            .filter(|entry| entry["id"] == CLAUDE_PROFILE_ID)
            .count();
        assert_eq!(friend_entries, 1);
    }

    #[test]
    fn claude_profile_maps_known_tier_and_leaves_opaque_models_explicit() {
        let profile = claude_profile(
            "https://gateway.example.com",
            "claude-fable-5",
            "secret-placeholder",
        );
        assert_eq!(profile["inferenceCredentialKind"], "static");
        assert_eq!(profile["inferenceModels"][0]["name"], "claude-fable-5");
        assert_eq!(
            profile["inferenceModels"][0]["anthropicFamilyTier"],
            "fable"
        );
        assert_eq!(profile["inferenceModels"][0]["isFamilyDefault"], true);

        let opaque = claude_model_entry("vendor-model");
        assert!(opaque
            .get("isFamilyDefault")
            .is_none());
    }

    #[test]
    fn codex_configuration_preserves_unrelated_settings_and_restores_selection() {
        let source = r#"
model = "official-model"
model_provider = "openai"
approval_policy = "on-request"

[mcp_servers.example]
command = "example"

[model_providers.friend_gateway]
name = "Previous Friend"
base_url = "https://old.example/v1"
"#;
        let mut document = source.parse::<DocumentMut>().expect("valid fixture");
        let restore = CodexRestore {
            model: string_value(&document, "model"),
            model_provider: string_value(&document, "model_provider"),
            previous_friend_provider: document
                .get("model_providers")
                .and_then(Item::as_table_like)
                .and_then(|providers| providers.get(CODEX_PROVIDER_ID))
                .map(ToString::to_string),
        };

        apply_codex_config(
            &mut document,
            "https://gateway.example.com",
            "gpt-test",
            Path::new("/Applications/Friend Codex.app/Contents/MacOS/friend-agent-launcher"),
        );
        assert_eq!(document["model"].as_str(), Some("gpt-test"));
        assert_eq!(
            document["model_providers"][CODEX_PROVIDER_ID]["base_url"].as_str(),
            Some("https://gateway.example.com/v1")
        );
        assert!(document["model_providers"][CODEX_PROVIDER_ID]
            .get("requires_openai_auth")
            .is_none());
        assert_eq!(
            document["mcp_servers"]["example"]["command"].as_str(),
            Some("example")
        );

        restore_codex_document(&mut document, restore).expect("restore should work");
        assert_eq!(document["model"].as_str(), Some("official-model"));
        assert_eq!(document["model_provider"].as_str(), Some("openai"));
        assert_eq!(
            document["model_providers"][CODEX_PROVIDER_ID]["name"].as_str(),
            Some("Previous Friend")
        );
        assert_eq!(
            document["mcp_servers"]["example"]["command"].as_str(),
            Some("example")
        );
    }

    #[test]
    fn transactional_write_replaces_existing_file() {
        let root =
            env::temp_dir().join(format!("friend-agent-launcher-test-{}", std::process::id()));
        fs::create_dir_all(&root).expect("create temp root");
        let path = root.join("config.txt");
        fs::write(&path, "old").expect("write fixture");
        write_text_atomic(&path, "new").expect("replace file");
        assert_eq!(fs::read_to_string(&path).expect("read result"), "new");
        let _ = fs::remove_dir_all(root);
    }
}
