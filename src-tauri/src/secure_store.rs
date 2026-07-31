use crate::gateway::CatalogDocument;
use std::{
    collections::HashMap,
    sync::{Mutex, OnceLock},
    time::{Duration, Instant},
};

const LOCAL_FLOW_TTL: Duration = Duration::from_secs(10 * 60);

pub(crate) struct FlowContext {
    pub(crate) secret: String,
    pub(crate) catalog: CatalogDocument,
}

struct FlowEntry {
    context: FlowContext,
    expires_at: Instant,
}

struct FlowStore {
    current_id: Option<String>,
    entries: HashMap<String, FlowEntry>,
}

static FLOW_STORE: OnceLock<Mutex<FlowStore>> = OnceLock::new();

fn store() -> &'static Mutex<FlowStore> {
    FLOW_STORE.get_or_init(|| {
        Mutex::new(FlowStore {
            current_id: None,
            entries: HashMap::new(),
        })
    })
}

fn with_store<T>(operation: impl FnOnce(&mut FlowStore) -> T) -> T {
    let mut guard = store()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    purge_expired(&mut guard);
    operation(&mut guard)
}

fn purge_expired(store: &mut FlowStore) {
    let now = Instant::now();
    store.entries.retain(|_, entry| entry.expires_at > now);
    if store
        .current_id
        .as_ref()
        .is_some_and(|current| !store.entries.contains_key(current))
    {
        store.current_id = None;
    }
}

pub(crate) fn replace(secret: String, catalog: CatalogDocument) -> Result<(), String> {
    validate_secret(&secret)?;
    let local_flow_id = new_opaque_id("flow")?;
    with_store(|store| {
        store.entries.clear();
        store.current_id = Some(local_flow_id.clone());
        store.entries.insert(
            local_flow_id,
            FlowEntry {
                context: FlowContext { secret, catalog },
                expires_at: Instant::now() + LOCAL_FLOW_TTL,
            },
        );
    });
    Ok(())
}

pub(crate) fn take_current() -> Option<FlowContext> {
    with_store(|store| {
        let current_id = store.current_id.take()?;
        store.entries.remove(&current_id).map(|entry| entry.context)
    })
}

pub(crate) fn clear() {
    with_store(|store| {
        store.current_id = None;
        store.entries.clear();
    });
}

pub(crate) fn validate_secret(secret: &str) -> Result<(), String> {
    crate::gateway::validate_secret(secret)
}

pub(crate) fn new_opaque_id(prefix: &str) -> Result<String, String> {
    let mut bytes = [0_u8; 32];
    getrandom::fill(&mut bytes).map_err(|_| "无法创建不可预测的本地标识".to_string())?;
    let encoded: String = bytes.iter().map(|byte| format!("{byte:02x}")).collect();
    Ok(format!("{prefix}-{encoded}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_flow_ids_are_opaque_and_not_reused() {
        let first = new_opaque_id("flow").expect("random id");
        let second = new_opaque_id("flow").expect("random id");
        assert_ne!(first, second);
        assert_eq!(first.len(), 5 + 64);
        assert!(first.starts_with("flow-"));
    }

    #[test]
    fn clearing_current_flow_removes_the_only_in_process_secret_holder() {
        clear();
        let result = take_current();
        assert!(result.is_none());
    }
}
