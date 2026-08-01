use reqwest::{blocking::Client, StatusCode, Url};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use time::{format_description::well_known::Rfc3339, OffsetDateTime};

pub(crate) const PRODUCT: &str = "claude";
pub(crate) const PROTOCOL: &str = "anthropic-messages";
pub(crate) const FIXED_GATEWAY_REF: &str = "friend-fixed-gateway";
pub(crate) const CATALOG_TRUST_BOUNDARY: &str = "tls-fixed-gateway";
pub(crate) const CATALOG_VERSION_PREFIX: &str = "v1a-";

/// V1A directory trust is transport/schema-bound, not cryptographic: the
/// client accepts only its fixed HTTPS origin (localhost in debug), this strict
/// wire schema, the V1A version prefix, and an unexpired response.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct CatalogDocument {
    pub(crate) product: String,
    pub(crate) protocol: String,
    pub(crate) catalog_version: String,
    pub(crate) expires_at: String,
    pub(crate) integrity: String,
    pub(crate) catalog: Vec<CatalogEntry>,
    pub(crate) balance: Balance,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct CatalogEntry {
    pub(crate) product: String,
    pub(crate) protocol: String,
    pub(crate) canonical_id: String,
    pub(crate) model_ref: String,
    pub(crate) gateway_ref: String,
    pub(crate) display_name: String,
    pub(crate) capabilities: Vec<String>,
    pub(crate) default: bool,
    pub(crate) catalog_version: String,
    pub(crate) expires_at: String,
    pub(crate) billing_label: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Balance {
    pub(crate) amount_minor: u64,
    pub(crate) currency: String,
    pub(crate) as_of: String,
    pub(crate) source: String,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct CatalogOption {
    pub(crate) canonical_id: String,
    pub(crate) display_name: String,
    pub(crate) capabilities: Vec<String>,
    pub(crate) billing_label: String,
    pub(crate) default: bool,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct FriendFlowView {
    pub(crate) catalog_version: String,
    pub(crate) expires_at: String,
    pub(crate) catalog: Vec<CatalogOption>,
    pub(crate) balance: Balance,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct BalanceResponse {
    product: String,
    balance: Balance,
}

#[derive(Debug, Serialize)]
struct CatalogRequest {
    product: &'static str,
    protocol: &'static str,
}

pub(crate) fn fixed_gateway_url() -> Result<Url, String> {
    let configured = option_env!("FRIEND_GATEWAY_URL")
        .map(str::trim)
        .filter(|value| !value.is_empty());
    if let Some(value) = configured {
        return validate_gateway_origin(value, cfg!(debug_assertions));
    }

    if cfg!(debug_assertions) {
        // Debug builds may exercise a local fixture without embedding a remote
        // endpoint. Release builds never receive this fallback.
        return validate_gateway_origin("http://127.0.0.1:3000", true);
    }

    Err("发行版未配置固定 Friend 网关，已失败关闭".into())
}

pub(crate) fn validate_gateway_origin(raw: &str, debug_build: bool) -> Result<Url, String> {
    let parsed = Url::parse(raw).map_err(|_| "固定网关配置无效".to_string())?;
    let host = parsed.host_str().unwrap_or_default();
    let local_host = matches!(host, "localhost" | "127.0.0.1" | "::1");
    let valid_scheme = if debug_build {
        local_host && matches!(parsed.scheme(), "http" | "https")
    } else {
        parsed.scheme() == "https"
    };
    if !valid_scheme
        || parsed.username() != ""
        || parsed.password().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || !matches!(parsed.path(), "" | "/")
    {
        return Err(
            "固定网关必须是无用户信息、无查询参数的 HTTPS origin；debug 仅允许 localhost".into(),
        );
    }
    Ok(parsed)
}

fn endpoint(path: &str) -> Result<Url, String> {
    let base = fixed_gateway_url()?;
    base.join(&format!("v1/{path}"))
        .map_err(|_| "固定网关路径配置无效".to_string())
}

fn request_id() -> Result<String, String> {
    let mut bytes = [0_u8; 16];
    getrandom::fill(&mut bytes).map_err(|_| "无法创建网关请求标识".to_string())?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn client() -> Result<Client, String> {
    Client::builder()
        .connect_timeout(std::time::Duration::from_secs(10))
        .timeout(std::time::Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "初始化固定网关连接失败".into())
}

fn http_error(status: StatusCode) -> String {
    let message = match status {
        StatusCode::UNAUTHORIZED => "AUTH_REQUIRED",
        StatusCode::FORBIDDEN => "KEY_REVOKED",
        StatusCode::GONE => "KEY_EXPIRED",
        StatusCode::BAD_REQUEST => "PRODUCT_PROTOCOL_MISMATCH",
        StatusCode::NOT_FOUND => "UPSTREAM_UNAVAILABLE",
        _ if status.is_server_error() => "UPSTREAM_UNAVAILABLE",
        _ => "GATEWAY_REQUEST_FAILED",
    };
    format!("{message}（HTTP {}）", status.as_u16())
}

pub(crate) fn validate_secret(secret: &str) -> Result<(), String> {
    if secret.trim().is_empty() || secret.len() > 20_000 || secret.contains(['\r', '\n', '\0']) {
        return Err("Key 格式无效".into());
    }
    Ok(())
}

pub(crate) fn fetch_catalog(secret: &str) -> Result<CatalogDocument, String> {
    validate_secret(secret)?;
    let url = endpoint("friend/catalog")?;
    let request_id = request_id()?;
    let response = client()?
        .post(url)
        .header("X-Request-Id", request_id)
        .bearer_auth(secret.trim())
        .json(&CatalogRequest {
            product: PRODUCT,
            protocol: PROTOCOL,
        })
        .send()
        .map_err(|_| "固定网关不可达".to_string())?;
    if !response.status().is_success() {
        return Err(http_error(response.status()));
    }
    let document = response
        .json::<CatalogDocument>()
        .map_err(|_| "受信目录格式无效".to_string())?;
    validate_catalog(&document)?;
    Ok(document)
}

pub(crate) fn fetch_balance(secret: &str) -> Result<Balance, String> {
    validate_secret(secret)?;
    let url = endpoint("friend/balance")?;
    let request_id = request_id()?;
    let response = client()?
        .get(url)
        .header("X-Request-Id", request_id)
        .bearer_auth(secret.trim())
        .send()
        .map_err(|_| "固定网关不可达".to_string())?;
    if !response.status().is_success() {
        return Err(http_error(response.status()));
    }
    let body = response
        .json::<BalanceResponse>()
        .map_err(|_| "余额响应格式无效".to_string())?;
    if body.product != PRODUCT {
        return Err("PRODUCT_PROTOCOL_MISMATCH".into());
    }
    validate_balance(&body.balance)?;
    Ok(body.balance)
}

pub(crate) fn validate_catalog(document: &CatalogDocument) -> Result<(), String> {
    validate_catalog_at(document, OffsetDateTime::now_utc())
}

pub(crate) fn validate_catalog_at(
    document: &CatalogDocument,
    now: OffsetDateTime,
) -> Result<(), String> {
    if document.product != PRODUCT || document.protocol != PROTOCOL {
        return Err("PRODUCT_PROTOCOL_MISMATCH".into());
    }
    if !document.catalog_version.starts_with(CATALOG_VERSION_PREFIX)
        || !opaque_token(&document.catalog_version, 64)
    {
        return Err("目录版本不受信".into());
    }
    if document.integrity != CATALOG_TRUST_BOUNDARY {
        return Err("CATALOG_UNTRUSTED".into());
    }
    let expires_at = parse_expiry(&document.expires_at)?;
    if expires_at <= now {
        return Err("CATALOG_EXPIRED".into());
    }
    validate_balance(&document.balance)?;
    if document.catalog.is_empty() {
        return Err("受信目录为空".into());
    }

    let mut canonical_ids = BTreeSet::new();
    let mut defaults = 0_u8;
    for entry in &document.catalog {
        if entry.product != PRODUCT
            || entry.protocol != PROTOCOL
            || entry.catalog_version != document.catalog_version
            || entry.expires_at != document.expires_at
            || entry.gateway_ref != FIXED_GATEWAY_REF
        {
            return Err("目录条目与产品、协议、版本或固定网关不匹配".into());
        }
        if !opaque_token(&entry.canonical_id, 128)
            || !model_ref(&entry.model_ref)
            || entry.display_name.trim().is_empty()
            || entry.display_name.len() > 128
            || entry.billing_label.trim().is_empty()
            || entry.billing_label.len() > 128
        {
            return Err("目录条目字段无效".into());
        }
        if !canonical_ids.insert(entry.canonical_id.clone()) {
            return Err("目录包含重复 canonical_id".into());
        }
        let capabilities: BTreeSet<&str> = entry.capabilities.iter().map(String::as_str).collect();
        if capabilities.len() != entry.capabilities.len()
            || !capabilities.contains("streaming")
            || capabilities
                .iter()
                .any(|capability| !matches!(*capability, "streaming" | "tool_use"))
        {
            return Err("目录能力字段不受信".into());
        }
        if entry.default {
            defaults = defaults.saturating_add(1);
        }
    }
    if defaults > 1 {
        return Err("目录只能有一个默认项".into());
    }
    Ok(())
}

fn validate_balance(balance: &Balance) -> Result<(), String> {
    if balance.currency.len() != 3
        || !balance
            .currency
            .chars()
            .all(|character| character.is_ascii_uppercase())
        || balance.source != "new-api"
        || parse_expiry(&balance.as_of).is_err()
    {
        return Err("余额字段无效".into());
    }
    Ok(())
}

fn opaque_token(value: &str, max_bytes: usize) -> bool {
    value
        .as_bytes()
        .first()
        .is_some_and(|byte| byte.is_ascii_alphanumeric())
        && value.len() <= max_bytes
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b':'))
}

fn model_ref(value: &str) -> bool {
    const PREFIX: &str = "friend-model:";
    value
        .strip_prefix(PREFIX)
        .is_some_and(|suffix| opaque_token(suffix, 111))
        && value.len() <= 128
}

fn parse_expiry(value: &str) -> Result<OffsetDateTime, String> {
    OffsetDateTime::parse(value, &Rfc3339).map_err(|_| "目录过期时间格式无效".to_string())
}

pub(crate) fn resolve_entry<'a>(
    document: &'a CatalogDocument,
    canonical_id: &str,
    catalog_version: &str,
) -> Result<&'a CatalogEntry, String> {
    validate_catalog(document)?;
    if catalog_version != document.catalog_version {
        return Err("目录版本已变化，请重新验证 Key".into());
    }
    document
        .catalog
        .iter()
        .find(|entry| entry.canonical_id == canonical_id)
        .ok_or_else(|| "未知的 canonical_id".into())
}

pub(crate) fn to_view(document: &CatalogDocument) -> FriendFlowView {
    FriendFlowView {
        catalog_version: document.catalog_version.clone(),
        expires_at: document.expires_at.clone(),
        catalog: document
            .catalog
            .iter()
            .map(|entry| CatalogOption {
                canonical_id: entry.canonical_id.clone(),
                display_name: entry.display_name.clone(),
                capabilities: entry.capabilities.clone(),
                billing_label: entry.billing_label.clone(),
                default: entry.default,
            })
            .collect(),
        balance: document.balance.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture(expires_at: &str) -> CatalogDocument {
        serde_json::from_value(serde_json::json!({
            "product": PRODUCT,
            "protocol": PROTOCOL,
            "catalog_version": "v1a-test-1",
            "expires_at": expires_at,
            "integrity": CATALOG_TRUST_BOUNDARY,
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
                "expires_at": expires_at,
                "billing_label": "按量计费"
            }],
            "balance": {
                "amount_minor": 1250,
                "currency": "CNY",
                "as_of": "2025-01-01T00:00:00Z",
                "source": "new-api"
            }
        }))
        .expect("catalog fixture")
    }

    #[test]
    fn catalog_requires_fixed_trust_boundary_and_strict_schema() {
        let now = OffsetDateTime::from_unix_timestamp(1_735_689_600).expect("timestamp");
        let expires = (now + time::Duration::hours(1))
            .format(&Rfc3339)
            .expect("format");
        let document = fixture(&expires);
        assert!(validate_catalog_at(&document, now).is_ok());

        let mut untrusted = document.clone();
        untrusted.integrity = "other-trust-boundary".into();
        assert_eq!(
            validate_catalog_at(&untrusted, now),
            Err("CATALOG_UNTRUSTED".into())
        );

        let with_extra_field = serde_json::json!({
            "product": PRODUCT,
            "protocol": PROTOCOL,
            "catalog_version": "v1a-test-1",
            "expires_at": expires,
            "integrity": CATALOG_TRUST_BOUNDARY,
            "unexpected_field": "not-accepted",
            "catalog": [],
            "balance": {
                "amount_minor": 1,
                "currency": "CNY",
                "as_of": "2025-01-01T00:00:00Z",
                "source": "new-api"
            }
        });
        assert!(serde_json::from_value::<CatalogDocument>(with_extra_field).is_err());

        let entry = serde_json::json!({
            "product": PRODUCT,
            "protocol": PROTOCOL,
            "canonical_id": "claude.default",
            "model_ref": "friend-model:claude-test",
            "gateway_ref": FIXED_GATEWAY_REF,
            "display_name": "Claude 默认",
            "capabilities": ["streaming", "tool_use"],
            "default": true,
            "catalog_version": "v1a-test-1",
            "expires_at": expires,
            "billing_label": "按量计费",
            "unexpected_field": true
        });
        assert!(serde_json::from_value::<CatalogEntry>(entry).is_err());
    }

    #[test]
    fn client_wire_shapes_use_only_fixed_request_fields_and_minor_balance_units() {
        let request = serde_json::to_value(CatalogRequest {
            product: PRODUCT,
            protocol: PROTOCOL,
        })
        .expect("catalog request");
        assert_eq!(
            request,
            serde_json::json!({
                "product": PRODUCT,
                "protocol": PROTOCOL
            })
        );

        let balance = Balance {
            amount_minor: 1250,
            currency: "CNY".into(),
            as_of: "2025-01-01T00:00:00Z".into(),
            source: "new-api".into(),
        };
        assert_eq!(
            serde_json::to_value(balance).expect("balance snapshot"),
            serde_json::json!({
                "amount_minor": 1250,
                "currency": "CNY",
                "as_of": "2025-01-01T00:00:00Z",
                "source": "new-api"
            })
        );
    }

    #[test]
    fn catalog_rejects_expiry_product_protocol_and_duplicate_entries() {
        let now = OffsetDateTime::from_unix_timestamp(1_735_689_600).expect("timestamp");
        let expired = (now - time::Duration::minutes(1))
            .format(&Rfc3339)
            .expect("format");
        assert_eq!(
            validate_catalog_at(&fixture(&expired), now),
            Err("CATALOG_EXPIRED".into())
        );

        let future = (now + time::Duration::hours(1))
            .format(&Rfc3339)
            .expect("format");
        let mut wrong_product = fixture(&future);
        wrong_product.product = "codex".into();
        assert_eq!(
            validate_catalog_at(&wrong_product, now),
            Err("PRODUCT_PROTOCOL_MISMATCH".into())
        );

        let mut duplicate = fixture(&future);
        duplicate.catalog.push(duplicate.catalog[0].clone());
        assert_eq!(
            validate_catalog_at(&duplicate, now),
            Err("目录包含重复 canonical_id".into())
        );
    }

    #[test]
    fn gateway_origin_is_fixed_and_debug_is_localhost_only() {
        assert!(validate_gateway_origin("https://gateway.example.com", false).is_ok());
        assert!(validate_gateway_origin("http://gateway.example.com", false).is_err());
        assert!(validate_gateway_origin("http://127.0.0.1:3000", true).is_ok());
        assert!(validate_gateway_origin("https://gateway.example.com", true).is_err());
        assert!(validate_gateway_origin("https://gateway.example.com/path", false).is_err());
    }

    #[cfg(not(debug_assertions))]
    #[test]
    fn release_without_gateway_build_config_fails_closed() {
        if option_env!("FRIEND_GATEWAY_URL").is_none() {
            assert!(fixed_gateway_url().is_err());
        }
    }
}
