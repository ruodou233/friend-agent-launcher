mod claude;
mod gateway;
mod recovery;
mod secure_store;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
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
const CODEX_PROVIDER_ID: &str = "friend_gateway";

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

#[derive(Debug, Serialize)]
struct LauncherStatus {
    official_app_installed: bool,
    official_app_running: bool,
    official_app_version: Option<String>,
    gateway_configured: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct BeginFriendFlowRequest {
    secret: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ConfigureRequest {
    canonical_id: String,
    catalog_version: String,
}

#[derive(Debug, Serialize)]
struct ConfigureResult {
    state: &'static str,
}

#[derive(Debug, Deserialize)]
struct ModelRequest {
    endpoint: String,
    secret: String,
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

#[cfg(windows)]
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
        .map_err(|_| "无法确定启动器数据目录".to_string())
}

fn restore_path(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(app_data_dir(app)?.join("restore.json"))
}

// Keychain access remains only for the old, unregistered Codex compatibility
// implementation. V1A Claude never calls these functions.
fn keyring_entry(product: Product) -> Result<keyring::Entry, String> {
    keyring::Entry::new(KEYRING_SERVICE, product.account())
        .map_err(|_| "无法打开系统凭据库".to_string())
}

fn get_secret(product: Product) -> Result<String, String> {
    keyring_entry(product)?
        .get_password()
        .map_err(|_| "还没有保存 Key，请先粘贴 Key".to_string())
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

#[allow(dead_code)]
fn validate_model(model: &str) -> Result<(), String> {
    if model.trim().is_empty() || model.len() > 200 || model.contains(['\r', '\n', '\t', ' ']) {
        return Err("模型名称格式无效".into());
    }
    Ok(())
}

fn api_url(endpoint: &str, path: &str) -> String {
    let base = endpoint.trim_end_matches('/');
    if base.ends_with("/v1") {
        format!("{base}/{path}")
    } else {
        format!("{base}/v1/{path}")
    }
}

#[allow(dead_code)]
fn api_base_url(endpoint: &str) -> String {
    let base = endpoint.trim_end_matches('/');
    if base.ends_with("/v1") {
        base.to_string()
    } else {
        format!("{base}/v1")
    }
}

fn limited_error(response: reqwest::blocking::Response, _secret: &str) -> String {
    // Do not include a server response body: old compatibility paths must not
    // turn an upstream echo into a Key-bearing UI error.
    let status = response.status();
    format!("HTTP {}", status.as_u16())
}

fn read_optional_text(path: &Path, label: &str) -> Result<Option<String>, String> {
    match fs::read_to_string(path) {
        Ok(text) => Ok(Some(text)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(_) => Err(format!("读取{label}失败")),
    }
}

fn write_text_atomic(path: &Path, text: &str) -> Result<(), String> {
    recovery::write_bytes_atomic(path, text.as_bytes())
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
        return None;
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
    #[cfg(not(any(target_os = "macos", windows)))]
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
    Ok(LauncherStatus {
        official_app_installed: official_app_installed(product),
        official_app_running: official_process_running(product),
        official_app_version: None,
        gateway_configured: product == Product::Claude && gateway::fixed_gateway_url().is_ok(),
    })
}

#[tauri::command]
fn begin_friend_flow(
    app: AppHandle,
    request: BeginFriendFlowRequest,
) -> Result<gateway::FriendFlowView, String> {
    secure_store::clear();
    if product(&app) != Product::Claude {
        return Err("Codex 新流程在 V1A 阶段未开放".into());
    }
    let secret = request.secret.trim().to_string();
    secure_store::validate_secret(&secret)?;
    let catalog = match gateway::fetch_catalog(&secret) {
        Ok(catalog) => catalog,
        Err(error) => {
            secure_store::clear();
            return Err(error);
        }
    };
    if let Err(error) = secure_store::replace(secret, catalog.clone()) {
        secure_store::clear();
        return Err(error);
    }
    Ok(gateway::to_view(&catalog))
}

#[tauri::command]
fn configure_and_launch(
    app: AppHandle,
    request: ConfigureRequest,
) -> Result<ConfigureResult, String> {
    if product(&app) != Product::Claude {
        secure_store::clear();
        return Err("Codex 新流程在 V1A 阶段未开放".into());
    }
    let context = secure_store::take_current()
        .ok_or_else(|| "本地配置会话已过期，请重新输入 Key".to_string())?;
    let result = (|| {
        let gateway_url = gateway::fixed_gateway_url()?.to_string();
        let entry = gateway::resolve_entry(
            &context.catalog,
            &request.canonical_id,
            &request.catalog_version,
        )?;
        if !official_app_installed(Product::Claude) {
            return Err("尚未安装原版 Claude，请先点击“下载原版 App”".into());
        }
        let paths = claude::default_paths()?;
        claude::configure(
            &paths,
            entry,
            &request.catalog_version,
            &gateway_url,
            &context.secret,
            || launch_official(Product::Claude),
        )
    })();
    // The context is dropped here on every branch; this explicit clear also
    // removes any replaced/expired flow entry without exposing its identifier.
    secure_store::clear();
    result.map(|_| ConfigureResult { state: "committed" })
}

#[tauri::command]
fn refresh_friend_balance(app: AppHandle) -> Result<gateway::Balance, String> {
    secure_store::clear();
    if product(&app) != Product::Claude {
        return Err("Codex 新流程在 V1A 阶段未开放".into());
    }
    let gateway_url = gateway::fixed_gateway_url()?.to_string();
    let paths = claude::default_paths()?;
    let secret = claude::current_friend_key(&paths, &gateway_url)?;
    let result = gateway::fetch_balance(&secret);
    drop(secret);
    secure_store::clear();
    result
}

#[tauri::command]
fn cancel_friend_flow() {
    secure_store::clear();
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

#[allow(dead_code)]
fn install_codex_credential_helper(app: &AppHandle) -> Result<PathBuf, String> {
    let destination = codex_credential_helper_path(app)?;
    let executable = env::current_exe().map_err(|_| "无法确定当前启动器路径".to_string())?;
    let bytes = fs::read(&executable).map_err(|_| "读取凭据 Helper 失败".to_string())?;
    write_text_or_bytes(&destination, &bytes)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&destination, fs::Permissions::from_mode(0o700))
            .map_err(|_| "设置凭据 Helper 权限失败".to_string())?;
    }
    Ok(destination)
}

#[allow(dead_code)]
fn write_text_or_bytes(path: &Path, bytes: &[u8]) -> Result<(), String> {
    recovery::write_bytes_atomic(path, bytes)
}

#[allow(dead_code)]
fn string_value(document: &DocumentMut, key: &str) -> Option<String> {
    document.get(key)?.as_str().map(str::to_string)
}

// Legacy Codex implementation retained only for source compatibility. It is
// not registered as a V1A command and the new Claude UI cannot reach it.
#[allow(dead_code)]
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
                .map_err(|_| "旧 Provider 备份无法解析".to_string())
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

#[allow(dead_code)]
fn configure_codex(app: &AppHandle, endpoint: &str, model: &str) -> Result<(), String> {
    let path = codex_config_path()?;
    let original = read_optional_text(&path, "Codex config.toml")?.unwrap_or_default();
    let mut document = if original.trim().is_empty() {
        DocumentMut::new()
    } else {
        original
            .parse::<DocumentMut>()
            .map_err(|_| "现有 Codex config.toml 无法解析".to_string())?
    };

    if !restore_path(app)?.exists() {
        let previous_friend_provider = document
            .get("model_providers")
            .and_then(Item::as_table_like)
            .and_then(|providers| providers.get(CODEX_PROVIDER_ID))
            .map(ToString::to_string);
        recovery::write_json_atomic(
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
            .map_err(|_| "打开原版 App 失败".to_string())?;
        for _ in 0..40 {
            if official_process_running(product) {
                return Ok(());
            }
            thread::sleep(Duration::from_millis(250));
        }
        return Err("原版 App 未能启动，配置未提交".into());
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
            .map_err(|_| "打开原版 App 失败".to_string())?;
        for _ in 0..40 {
            if official_process_running(product) {
                return Ok(());
            }
            thread::sleep(Duration::from_millis(250));
        }
        return Err("原版 App 未能启动，配置未提交".into());
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
    Err("原版 App 正在运行且未能自动退出，请先完全退出后重试".into())
}

fn read_optional_models(endpoint: &str, secret: &str) -> Result<Vec<String>, String> {
    validate_endpoint(endpoint)?;
    secure_store::validate_secret(secret)?;
    let client = reqwest::blocking::Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "初始化模型列表失败".to_string())?;
    let response = client
        .get(api_url(endpoint, "models"))
        .bearer_auth(secret.trim())
        .send()
        .map_err(|_| "获取模型列表失败".to_string())?;
    if !response.status().is_success() {
        return Err(format!(
            "获取模型列表失败：{}",
            limited_error(response, secret)
        ));
    }
    let body: Value = response
        .json()
        .map_err(|_| "模型列表格式无效".to_string())?;
    let models = body
        .get("data")
        .and_then(Value::as_array)
        .ok_or("模型列表缺少 data 数组")?;
    let mut unique = std::collections::BTreeSet::new();
    for item in models {
        if let Some(id) = item.get("id").and_then(Value::as_str) {
            if !id.trim().is_empty() {
                unique.insert(id.trim().to_string());
            }
        }
    }
    if unique.is_empty() {
        return Err("模型列表为空".into());
    }
    Ok(unique.into_iter().collect())
}

// Kept unregistered so older Codex source integrations still type-check, but
// V1A never exposes arbitrary model discovery through the Tauri command table.
#[allow(dead_code)]
fn discover_models_legacy(request: ModelRequest) -> Result<Vec<String>, String> {
    let secret = if request.secret.trim().is_empty() {
        return Err("旧 Codex 兼容路径需要 Key".into());
    } else {
        request.secret.trim().to_string()
    };
    read_optional_models(&request.endpoint, &secret)
}

fn restore_codex(app: &AppHandle) -> Result<bool, String> {
    let restore_file = restore_path(app)?;
    if !restore_file.exists() {
        return Ok(false);
    }
    let restore: CodexRestore = serde_json::from_slice(
        &fs::read(&restore_file).map_err(|_| "读取恢复信息失败".to_string())?,
    )
    .map_err(|_| "恢复信息损坏".to_string())?;
    let path = codex_config_path()?;
    let current = read_optional_text(&path, "当前 Codex config.toml")?.unwrap_or_default();
    let mut document = current
        .parse::<DocumentMut>()
        .map_err(|_| "当前 Codex config.toml 无法解析".to_string())?;
    restore_codex_document(&mut document, restore)?;
    write_text_atomic(&path, &document.to_string())?;
    fs::remove_file(restore_file).map_err(|_| "清理恢复信息失败".to_string())?;
    Ok(true)
}

#[tauri::command]
fn restore_official_mode(app: AppHandle) -> Result<bool, String> {
    secure_store::clear();
    let product = product(&app);
    let restored = match product {
        Product::Claude => {
            let paths = claude::default_paths()?;
            claude::restore(&paths)
        }
        Product::Codex => restore_codex(&app),
    }?;
    if restored && official_app_installed(product) {
        launch_official(product)?;
    }
    if restored && product == Product::Codex {
        let _ = keyring_entry(product).and_then(|entry| {
            entry
                .delete_credential()
                .map_err(|_| "清理系统凭据失败".to_string())
        });
        if let Ok(path) = codex_credential_helper_path(&app) {
            let _ = fs::remove_file(path);
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
    open::that(url).map_err(|_| "打开系统浏览器失败".into())
}

pub fn run_credential_helper_if_requested() -> bool {
    let arguments: Vec<String> = env::args().collect();
    if arguments.get(1).map(String::as_str) != Some("--credential-helper") {
        return false;
    }
    let requested = arguments.get(2).map(String::as_str).unwrap_or_default();
    if requested == "claude" {
        eprintln!("V1A Claude 不使用凭据 Helper");
        std::process::exit(1);
    }
    let product = match requested {
        "codex" => Product::Codex,
        _ => product_from_executable(),
    };
    if product == Product::Claude {
        eprintln!("V1A Claude 不使用凭据 Helper");
        std::process::exit(1);
    }
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
            begin_friend_flow,
            configure_and_launch,
            refresh_friend_balance,
            cancel_friend_flow,
            restore_official_mode,
            open_allowed_link
        ])
        .run(tauri::generate_context!())
        .expect("Friend Agent Launcher 启动失败");
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

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
    fn configure_request_rejects_endpoint_model_and_extra_secret_fields() {
        assert!(serde_json::from_value::<ConfigureRequest>(json!({
            "canonical_id": "claude.default",
            "catalog_version": "v1a-test-1"
        }))
        .is_ok());
        assert!(serde_json::from_value::<ConfigureRequest>(json!({
            "canonical_id": "claude.default",
            "catalog_version": "v1a-test-1",
            "endpoint": "https://untrusted.example",
            "model": "raw-model"
        }))
        .is_err());
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
