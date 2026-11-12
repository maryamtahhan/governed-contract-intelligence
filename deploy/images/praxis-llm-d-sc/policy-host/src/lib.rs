// gci-praxis-policy-host — host registrations for the PPE plugins the GCI
// gateway runs: the PII scanner (observe and gate modes) and the step-key
// binding gate.
//
// The registration function below is called by the code the ai-gateway
// server's build.rs generates for every discovered
// `[package.metadata.praxis-filters]` crate — the same path that registers
// the llm_d_sc filter — so it runs at startup, before any policy filter
// constructs (and again reads the registry on every hot reload; the host
// table is never drained, by design).
//
// The PII scanner mirrors the reference `validator/pii-scan`
// (praxis-proxy/policy, reference/plugins/pii-scanner) in structure —
// factory, hook fan-out, Text/argument scanning — and its pattern catalog
// mirrors demo/src/hybrid_demo/pii.py (PII_PATTERNS) regex-for-regex, entity
// name for entity name, so the agreement report compares like with like. A
// divergence here is a bug in one side or the other, not a finding.
//
// The observe kind still never denies and never rewrites: it is the
// comparison instrument. The gate kind uses the same scanner and denies on a
// match; the general review entry runs it so a client cannot bypass the
// pipeline's sensitive-entry decision. The screen entry deliberately loads
// the observe-only policy because its input is the document being screened.

use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
};

use praxis_policy_core::{
    cmf::{CmfHook, ContentPart, Message, MessagePayload},
    context::PluginContext,
    error::{PluginError, PluginViolation},
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

/// `kind:` operators write in PPE YAML to declare the enforcing scanner.
pub const PII_GATE_KIND: &str = "validator/gci-pii-gate";

/// `kind:` operators write in PPE YAML to bind an API-key subject to the
/// stage in `X-Demo-Step`.
pub const STEP_KEY_KIND: &str = "validator/gci-step-key";

const DEFAULT_STEP_HEADER: &str = "X-Demo-Step";
const DEFAULT_ROLE_PREFIX: &str = "step:";

/// The demo screen's pattern catalog (demo/src/hybrid_demo/pii.py,
/// PII_PATTERNS), mirrored exactly — same regexes, same entity names, same
/// case sensitivity — so the gateway-side measurement and the pipeline's
/// screen answer the same question. Rust's `regex` syntax covers every
/// pattern here (no lookaround involved).
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
/// no HTTP filters, only the policy plugin factories.
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
    for kind in [PII_OBSERVE_KIND, PII_GATE_KIND] {
        praxis_filter::register_policy_plugin_factory(
            kind,
            Arc::new(|| Box::new(GciPiiFactory)),
        );
    }
    praxis_filter::register_policy_plugin_factory(
        STEP_KEY_KIND,
        Arc::new(|| Box::new(GciStepKeyFactory)),
    );
}

/// Plugin settings: which entities to scan for. Defaults to the full
/// catalog. An unknown name fails the load — a typo must not silently
/// narrow the screen.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct PiiConfig {
    /// Entity names from the catalog; `None` means all of them.
    #[serde(default)]
    detect: Option<Vec<String>>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PiiAction {
    Observe,
    Gate,
}

impl PiiAction {
    fn from_kind(kind: &str) -> Option<Self> {
        match kind {
            PII_OBSERVE_KIND => Some(Self::Observe),
            PII_GATE_KIND => Some(Self::Gate),
            _ => None,
        }
    }

    fn log_plugin(self) -> &'static str {
        match self {
            Self::Observe => "gci_pii_observe",
            Self::Gate => "gci_pii_gate",
        }
    }

    fn denies(self, matched: &[String]) -> bool {
        self == Self::Gate && !matched.is_empty()
    }
}

/// Factory shared by both scanner kinds. `config.kind` selects the action;
/// both register the same handler on every CMF hook named in `cfg.hooks`.
pub struct GciPiiFactory;

impl PluginFactory for GciPiiFactory {
    fn create(&self, config: &PluginConfig) -> Result<PluginInstance, Box<PluginError>> {
        let action = PiiAction::from_kind(&config.kind).ok_or_else(|| {
            Box::new(PluginError::Config {
                message: format!(
                    "plugin '{}': factory registered for the PII scanner but kind is '{}'",
                    config.name, config.kind
                ),
            })
        })?;
        let scanner = Arc::new(GciPiiScanner::new(config.clone(), action)?);
        if config.hooks.is_empty() {
            return Err(Box::new(PluginError::Config {
                message: format!(
                    "plugin '{}' ({}): `hooks:` must list at least one CMF hook to scan on \
                     (e.g. cmf.llm_input)",
                    config.name, config.kind
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

/// The scanner. Holds the compiled pattern set and the action selected by
/// its registered kind; every invocation logs one line. Observe always
/// allows; gate denies when any entity matches.
pub struct GciPiiScanner {
    cfg: PluginConfig,
    patterns: Vec<(String, Regex)>,
    action: PiiAction,
}

impl GciPiiScanner {
    /// # Errors
    ///
    /// `PluginError::Config` when `config:` is present but does not
    /// deserialize, or names an entity outside the catalog.
    fn new(cfg: PluginConfig, action: PiiAction) -> Result<Self, Box<PluginError>> {
        let typed = match cfg.config.as_ref() {
            None => PiiConfig::default(),
            Some(raw) => serde_json::from_value(raw.clone()).map_err(|e| {
                Box::new(PluginError::Config {
                    message: format!(
                        "plugin '{}' ({}): config parse failed: {e}",
                        cfg.name, cfg.kind
                    ),
                })
            })?,
        };
        let selected: Vec<&str> = match &typed.detect {
            None => PATTERNS.iter().map(|(name, _)| *name).collect(),
            Some(names) => {
                for name in names {
                    if !PATTERNS.iter().any(|(n, _)| *n == name.as_str()) {
                        return Err(Box::new(PluginError::Config {
                            message: format!(
                                "plugin '{}' ({}): unknown entity '{name}' in detect:; the \
                                 catalog is: {}",
                                cfg.name,
                                cfg.kind,
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
            .filter(|(name, _)| selected.contains(name))
            .filter_map(|(name, re)| {
                Regex::new(re)
                    .ok()
                    .map(|compiled| ((*name).to_owned(), compiled))
            })
            .collect();
        Ok(Self {
            cfg,
            patterns,
            action,
        })
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

impl Plugin for GciPiiScanner {
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
impl HookHandler<CmfHook> for GciPiiScanner {
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
        let step = ext
            .http
            .as_ref()
            .and_then(|h| h.get_request_header(DEFAULT_STEP_HEADER))
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
        let denied = self.action.denies(&matched);
        let outcome = match self.action {
            PiiAction::Observe => "observe",
            PiiAction::Gate if denied => "deny",
            PiiAction::Gate => "allow",
        };
        // One self-contained JSON line as the event message, so the line is
        // parseable regardless of how the host's tracing subscriber formats
        // fields. The agreement tool accepts both plugin markers.
        let line = serde_json::json!({
            "plugin": self.action.log_plugin(),
            "action": outcome,
            "session_id": &session,
            "subject": &subject,
            "step": &step,
            "matched": &matched,
            "text_chars": text_chars,
        });
        match self.action {
            PiiAction::Observe => tracing::info!(target: "gci_pii_observe", "{line}"),
            PiiAction::Gate => tracing::info!(target: "gci_pii_gate", "{line}"),
        }
        if denied {
            let mut details = HashMap::new();
            if !subject.is_empty() {
                details.insert("subject".to_owned(), Value::String(subject));
            }
            if !step.is_empty() {
                details.insert("step".to_owned(), Value::String(step.to_owned()));
            }
            let reason = format!(
                "PII detected on the general entry: {}",
                matched.join(", ")
            );
            return PluginResult::deny(
                PluginViolation::new("pii_detected", reason)
                    .with_details(details)
                    .with_proto_error_code(403),
            );
        }
        PluginResult::allow()
    }
}

/// Settings for the step-key gate. The API-key directory maps each key's
/// `groups` into `subject.roles`; a group named `step:<stage>` is the grant
/// for that stage. A key with an `eval` role may make a step-less
/// classifier/eval call, but it cannot claim a review step.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct StepKeyConfig {
    /// Header carrying the routing step, e.g. `classify` or
    /// `extract:MSA-1.1`.
    #[serde(default = "default_step_header")]
    header: String,
    /// Prefix that turns a step stage into the role name.
    #[serde(default = "default_role_prefix")]
    role_prefix: String,
    /// Roles allowed to omit the step header entirely. The eval harness is
    /// the one caller that does this; it never names a pool.
    #[serde(default)]
    stepless_roles: Vec<String>,
}

impl Default for StepKeyConfig {
    fn default() -> Self {
        Self {
            header: default_step_header(),
            role_prefix: default_role_prefix(),
            stepless_roles: Vec::new(),
        }
    }
}

fn default_step_header() -> String {
    DEFAULT_STEP_HEADER.to_owned()
}

fn default_role_prefix() -> String {
    DEFAULT_ROLE_PREFIX.to_owned()
}

/// Factory for [`STEP_KEY_KIND`]. Registers one handler per CMF hook named
/// in `cfg.hooks`, same as the scanner.
pub struct GciStepKeyFactory;

impl PluginFactory for GciStepKeyFactory {
    fn create(&self, config: &PluginConfig) -> Result<PluginInstance, Box<PluginError>> {
        let gate = Arc::new(GciStepKey::new(config.clone())?);
        if config.hooks.is_empty() {
            return Err(Box::new(PluginError::Config {
                message: format!(
                    "plugin '{}' ({}): `hooks:` must list at least one CMF hook to scan on \
                     (e.g. cmf.llm_input)",
                    config.name, config.kind
                ),
            }));
        }
        let handlers: Vec<_> = config
            .hooks
            .iter()
            .map(|h| -> (&'static str, _) {
                let leaked: &'static str = Box::leak(h.clone().into_boxed_str());
                let adapter: Arc<dyn praxis_policy_core::registry::AnyHookHandler> =
                    Arc::new(TypedHandlerAdapter::<CmfHook, _>::new(Arc::clone(&gate)));
                (leaked, adapter)
            })
            .collect();
        Ok(PluginInstance {
            plugin: gate,
            handlers,
        })
    }
}

/// The step-key gate. Fails closed when the authenticated subject is absent,
/// when a non-exempt caller omits the step header, or when the step's stage
/// is not one of the subject's `step:<stage>` roles.
pub struct GciStepKey {
    cfg: PluginConfig,
    settings: StepKeyConfig,
}

impl GciStepKey {
    /// # Errors
    ///
    /// `PluginError::Config` when `config:` is present but does not
    /// deserialize, or when the header/prefix would be empty.
    fn new(cfg: PluginConfig) -> Result<Self, Box<PluginError>> {
        let settings = match cfg.config.as_ref() {
            None => StepKeyConfig::default(),
            Some(raw) => serde_json::from_value(raw.clone()).map_err(|e| {
                Box::new(PluginError::Config {
                    message: format!(
                        "plugin '{}' ({}): config parse failed: {e}",
                        cfg.name, cfg.kind
                    ),
                })
            })?,
        };
        if settings.header.trim().is_empty() {
            return Err(Box::new(PluginError::Config {
                message: format!(
                    "plugin '{}' ({}): `header:` must name the step header",
                    cfg.name, cfg.kind
                ),
            }));
        }
        if settings.role_prefix.trim().is_empty() {
            return Err(Box::new(PluginError::Config {
                message: format!(
                    "plugin '{}' ({}): `role_prefix:` must not be empty",
                    cfg.name, cfg.kind
                ),
            }));
        }
        Ok(Self { cfg, settings })
    }
}

impl Plugin for GciStepKey {
    fn config(&self) -> &PluginConfig {
        &self.cfg
    }
}

impl HookHandler<CmfHook> for GciStepKey {
    async fn handle(
        &self,
        _payload: &MessagePayload,
        ext: &Extensions,
        _ctx: &mut PluginContext,
    ) -> PluginResult<MessagePayload> {
        let subject = ext
            .security
            .as_ref()
            .and_then(|s| s.subject.as_ref());
        let subject_id = subject
            .and_then(|s| s.id.clone())
            .unwrap_or_default();
        let step = ext
            .http
            .as_ref()
            .and_then(|h| h.get_request_header(&self.settings.header))
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_owned);
        let outcome = match subject {
            None => Err((
                "subject_missing".to_owned(),
                "step binding needs an authenticated subject".to_owned(),
            )),
            Some(subject) => check_step_roles(&subject.roles, step.as_deref(), &self.settings),
        };
        let (decision, code) = match &outcome {
            Ok(()) => ("allow", ""),
            Err((code, _)) => ("deny", code.as_str()),
        };
        let line = serde_json::json!({
            "plugin": "gci_step_key",
            "subject": &subject_id,
            "step": step.as_deref().unwrap_or_default(),
            "decision": decision,
            "code": code,
        });
        tracing::info!(target: "gci_step_key", "{line}");
        match outcome {
            Ok(()) => PluginResult::allow(),
            Err((code, reason)) => PluginResult::deny(
                PluginViolation::new(code, reason)
                    .with_proto_error_code(403),
            ),
        }
    }
}

/// The role check, separated from the hook so it can be tested without an
/// `Extensions` value. A step header's stage is the text before the first
/// `:`; the subject must carry `role_prefix + stage`.
fn check_step_roles(
    roles: &HashSet<String>,
    step: Option<&str>,
    settings: &StepKeyConfig,
) -> Result<(), (String, String)> {
    match step {
        Some(step) => {
            let stage = step.split_once(':').map_or(step, |(stage, _)| stage).trim();
            if stage.is_empty() {
                return Err((
                    "step_invalid".to_owned(),
                    "the step header has an empty stage".to_owned(),
                ));
            }
            let required = format!("{}{}", settings.role_prefix, stage);
            if roles.contains(&required) {
                Ok(())
            } else {
                Err((
                    "step_key_mismatch".to_owned(),
                    format!("subject is not granted the '{required}' role"),
                ))
            }
        }
        None => {
            if settings
                .stepless_roles
                .iter()
                .any(|role| roles.contains(role))
            {
                Ok(())
            } else {
                Err((
                    "step_missing".to_owned(),
                    "this key may not omit the step header".to_owned(),
                ))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn roles(values: &[&str]) -> HashSet<String> {
        values.iter().map(|v| (*v).to_owned()).collect()
    }

    fn settings() -> StepKeyConfig {
        StepKeyConfig {
            header: DEFAULT_STEP_HEADER.to_owned(),
            role_prefix: DEFAULT_ROLE_PREFIX.to_owned(),
            stepless_roles: vec!["eval".to_owned()],
        }
    }

    #[test]
    fn a_step_is_granted_by_its_stage_role() {
        assert!(
            check_step_roles(&roles(&["step:extract"]), Some("extract:MSA-1.1"), &settings())
                .is_ok()
        );
        assert!(
            check_step_roles(&roles(&["step:classify"]), Some("extract:MSA-1.1"), &settings())
                .is_err()
        );
    }

    #[test]
    fn an_eval_key_can_be_stepless_but_cannot_claim_a_step() {
        assert!(check_step_roles(&roles(&["eval"]), None, &settings()).is_ok());
        assert!(check_step_roles(&roles(&["eval"]), Some("risk"), &settings()).is_err());
    }

    #[test]
    fn empty_steps_are_rejected() {
        assert!(check_step_roles(&roles(&["step:"]), Some(":"), &settings()).is_err());
    }

    #[test]
    fn only_the_gate_denies_on_a_match() {
        let matched = vec!["EMAIL_ADDRESS".to_owned()];
        assert!(!PiiAction::Observe.denies(&matched));
        assert!(PiiAction::Gate.denies(&matched));
        assert!(!PiiAction::Gate.denies(&[]));
    }
}
