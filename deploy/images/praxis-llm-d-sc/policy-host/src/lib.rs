// gci-praxis-policy-host — host registrations for the PPE plugins the GCI
// gateway runs, plus the observe-only PII scanner.
//
// The registration function below is called by the code the ai-gateway
// server's build.rs generates for every discovered
// `[package.metadata.praxis-filters]` crate — the same path that registers
// the llm_d_sc filter — so it runs at startup, before any policy filter
// constructs (and again reads the registry on every hot reload; the host
// table is never drained, by design).
//
// The scanner mirrors the reference `validator/pii-scan`
// (praxis-proxy/policy, reference/plugins/pii-scanner) in structure —
// factory, hook fan-out, Text/argument scanning — with two deliberate
// differences:
//
//   * it is observe-only. The reference's modes are `deny` and `redact`;
//     neither belongs in a first deployment, whose job is to be measured
//     against the pipeline's client-side screen, not to act. This plugin
//     always returns allow and logs what it found.
//   * its pattern catalog mirrors demo/src/hybrid_demo/pii.py
//     (PII_PATTERNS) regex-for-regex, entity name for entity name, so the
//     agreement report compares like with like. A divergence here is a
//     bug in one side or the other, not a finding.

use std::sync::Arc;

use praxis_policy_core::{
    cmf::{CmfHook, ContentPart, Message, MessagePayload},
    context::PluginContext,
    error::PluginError,
    factory::{PluginFactory, PluginInstance},
    hooks::{
        payload::Extensions,
        trait_def::{HookHandler, PluginResult},
        TypedHandlerAdapter,
    },
    plugin::{Plugin, PluginConfig},
};
use regex::Regex;
use serde::Deserialize;
use serde_json::Value;

/// `kind:` operators write in PPE YAML to declare the observe-only scanner.
pub const PII_OBSERVE_KIND: &str = "validator/gci-pii-observe";

/// The demo screen's pattern catalog (demo/src/hybrid_demo/pii.py,
/// PII_PATTERNS), mirrored exactly — same regexes, same entity names, same
/// case sensitivity — so the gateway-side shadow measurement and the
/// pipeline's screen answer the same question. Rust's `regex` syntax
/// covers every pattern here (no lookaround involved).
const PATTERNS: &[(&str, &str)] = &[
    ("EMAIL_ADDRESS", r"[\w.+-]+@[\w-]+\.[\w.]+"),
    ("EMPLOYEE_ID", r"\bEMP-\d{4,}\b"),
    ("ACCOUNT_ID", r"\bACCT-\d{4,}\b"),
    ("CUSTOMER_ID", r"\bCUST-\d{4,}\b"),
    ("NATIONAL_ID", r"\b[A-Z]{2}\d{2}[A-Z0-9]{8,}\b"),
    ("PERSONAL_DATA_MARKER", r"personal data"),
    ("DATA_CONTROLLER_MARKER", r"data controller"),
    ("DATA_PROCESSOR_MARKER", r"data processor"),
    ("SPECIAL_CATEGORY_MARKER", r"special categories of personal data"),
    ("PASSPORT_MARKER", r"passport"),
    ("TAX_ID_MARKER", r"tax identification"),
    ("IDENTIFICATION_MARKER", r"identification number"),
    ("PHONE_NUMBER", r"\+\d{1,3}[\s-]\d"),
];

/// Host registration, invoked at startup by the generated external-filter
/// registration. The registry parameter is unused: this crate contributes
/// no HTTP filters, only the two policy plugin factories.
///
/// # Panics
///
/// Panics only through the (impossible here) filter-name collision path
/// the generated caller documents; the policy registrations themselves
/// are infallible map inserts.
pub fn register_filters(_registry: &mut praxis_filter::FilterRegistry) {
    // The reference audit sink, vendored next to this crate by build.sh.
    // Registered first so a policy naming it fails loudly at load if the
    // vendoring ever drifts, not silently at first request.
    praxis_filter::register_policy_plugin_factory(
        praxis_policy_plugin_audit_logger::KIND,
        Arc::new(|| Box::new(praxis_policy_plugin_audit_logger::AuditLoggerFactory)),
    );
    praxis_filter::register_policy_plugin_factory(
        PII_OBSERVE_KIND,
        Arc::new(|| Box::new(GciPiiObserveFactory)),
    );
}

/// Plugin settings: which entities to scan for. Defaults to the full
/// catalog. An unknown name fails the load — a typo must not silently
/// narrow the screen.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct ObserveConfig {
    /// Entity names from the catalog; `None` means all of them.
    #[serde(default)]
    detect: Option<Vec<String>>,
}

/// Factory for [`PII_OBSERVE_KIND`]. Mirrors the reference scanner's
/// factory: instantiates from the `config:` block and registers one
/// handler per CMF hook name declared in `cfg.hooks` (empty is a config
/// error — a scanner wired to nothing would look deployed and see nothing).
pub struct GciPiiObserveFactory;

impl PluginFactory for GciPiiObserveFactory {
    fn create(&self, config: &PluginConfig) -> Result<PluginInstance, Box<PluginError>> {
        let scanner = Arc::new(GciPiiObserve::new(config.clone())?);
        if config.hooks.is_empty() {
            return Err(Box::new(PluginError::Config {
                message: format!(
                    "plugin '{}' ({}): `hooks:` must list at least one CMF hook to scan on \
                     (e.g. cmf.llm_input)",
                    config.name, PII_OBSERVE_KIND
                ),
            }));
        }
        let handlers: Vec<_> = config
            .hooks
            .iter()
            .map(|h| -> (&'static str, _) {
                // Same lifetime approach as the reference factory: the
                // hook registry stores 'static names, plugin configs are
                // read once at startup, and the bound is plugin × hook
                // pairs in config (small).
                let leaked: &'static str = Box::leak(h.clone().into_boxed_str());
                let adapter: Arc<dyn praxis_policy_core::registry::AnyHookHandler> =
                    Arc::new(TypedHandlerAdapter::<CmfHook, _>::new(Arc::clone(&scanner)));
                (leaked, adapter)
            })
            .collect();
        Ok(PluginInstance {
            plugin: scanner,
            handlers,
        })
    }
}

/// The observe-only scanner. Holds the compiled pattern set; every
/// invocation logs one line and allows.
pub struct GciPiiObserve {
    cfg: PluginConfig,
    patterns: Vec<(String, Regex)>,
}

impl GciPiiObserve {
    /// # Errors
    ///
    /// `PluginError::Config` when `config:` is present but does not
    /// deserialize, or names an entity outside the catalog.
    fn new(cfg: PluginConfig) -> Result<Self, Box<PluginError>> {
        let typed = match cfg.config.as_ref() {
            None => ObserveConfig::default(),
            Some(raw) => serde_json::from_value(raw.clone()).map_err(|e| {
                Box::new(PluginError::Config {
                    message: format!(
                        "plugin '{}' ({}): config parse failed: {e}",
                        cfg.name, PII_OBSERVE_KIND
                    ),
                })
            })?,
        };
        let selected: Vec<&str> = match &typed.detect {
            None => PATTERNS.iter().map(|(name, _)| *name).collect(),
            Some(names) => {
                for name in names {
                    if !PATTERNS.iter().any(|(n, _)| n == name) {
                        return Err(Box::new(PluginError::Config {
                            message: format!(
                                "plugin '{}' ({}): unknown entity '{name}' in detect:; the \
                                 catalog is: {}",
                                cfg.name,
                                PII_OBSERVE_KIND,
                                PATTERNS.iter().map(|(n, _)| *n).collect::<Vec<_>>().join(", ")
                            ),
                        }));
                    }
                }
                names.iter().map(|n| n.as_str()).collect()
            }
        };
        let patterns = PATTERNS
            .iter()
            .filter(|(name, _)| selected.contains(&name))
            .filter_map(|(name, re)| {
                Regex::new(re)
                    .ok()
                    .map(|compiled| ((*name).to_owned(), compiled))
            })
            .collect();
        Ok(Self { cfg, patterns })
    }

    /// Every matching entity name across the message's text parts and
    /// argument maps, deduplicated. All matches, not just the first: the
    /// agreement report wants per-entity signal, and a single entity
    /// deciding the pipeline's screen means one shared false positive is
    /// indistinguishable from a real one unless the names are all there.
    fn matches(&self, message: &Message) -> Vec<String> {
        let mut out: Vec<String> = Vec::new();
        for part in &message.content {
            match part {
                ContentPart::Text { text } => self.match_str(text, &mut out),
                ContentPart::ToolCall { content } => {
                    for v in content.arguments.values() {
                        if let Value::String(s) = v {
                            self.match_str(s, &mut out);
                        }
                    }
                }
                ContentPart::PromptRequest { content } => {
                    for v in content.arguments.values() {
                        if let Value::String(s) = v {
                            self.match_str(s, &mut out);
                        }
                    }
                }
                // Images / media are out of scope, same as the reference.
                _ => {}
            }
        }
        out.sort();
        out.dedup();
        out
    }

    fn match_str(&self, s: &str, out: &mut Vec<String>) {
        for (name, re) in &self.patterns {
            if re.is_match(s) {
                out.push(name.clone());
            }
        }
    }
}

impl Plugin for GciPiiObserve {
    fn config(&self) -> &PluginConfig {
        &self.cfg
    }
}

// No #[async_trait] here: praxis-policy-core 0.4.1 declares HookHandler
// with RPITIT (`-> impl Future<Output = H::Result> + Send`), and the
// attribute's boxed-future desugaring does not match it (E0195, the
// second v0.9.1 build failure). A bare `async fn` is the matching form —
// same as the v0.4.1 reference scanner. If a praxis release moves the
// trait to async-trait, this and the import come back.
impl HookHandler<CmfHook> for GciPiiObserve {
    async fn handle(
        &self,
        payload: &MessagePayload,
        ext: &Extensions,
        _ctx: &mut PluginContext,
    ) -> PluginResult<MessagePayload> {
        let matched = self.matches(&payload.message);
        let session = ext
            .agent
            .as_ref()
            .and_then(|a| a.session_id.clone())
            .unwrap_or_default();
        let subject = ext
            .security
            .as_ref()
            .and_then(|s| s.subject.as_ref())
            .and_then(|s| s.id.clone())
            .unwrap_or_default();
        let text_chars: usize = payload
            .message
            .content
            .iter()
            .map(|p| match p {
                ContentPart::Text { text } => text.chars().count(),
                _ => 0,
            })
            .sum();
        // One self-contained JSON line as the event message, so the line is
        // parseable regardless of how the host's tracing subscriber formats
        // fields. The analysis tool greps for the plugin field.
        let line = serde_json::json!({
            "plugin": "gci_pii_observe",
            "session_id": session,
            "subject": subject,
            "matched": matched,
            "text_chars": text_chars,
        });
        tracing::info!(target: "gci_pii_observe", "{line}");
        // Observe-only, by design: never deny, never modify. Promoting the
        // scanner to a gate is a measured decision that belongs to a
        // future config, not to this plugin.
        PluginResult::allow()
    }
}
