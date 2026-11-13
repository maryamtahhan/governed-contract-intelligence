//! `gci_screen` — the V19 in-gateway sensitivity decision.
//!
//! A Praxis HTTP filter that scans an OpenAI-shaped request body with the
//! demo's PII pattern catalog (the same thirteen regexes
//! `demo/src/hybrid_demo/pii.py` and the `gci-pii-*` policy plugins carry)
//! and exposes the verdict as a request header the router can match:
//!
//! ```yaml
//! - filter: gci_screen
//!   header: X-Demo-Screen          # default
//!   sensitive_value: sensitive     # default
//!   general_value: general         # default
//! ```
//!
//! Contract:
//!
//! * the request body is never modified — the filter is read-only on the
//!   payload, exactly like the policy scanners it mirrors;
//! * a client-supplied routing header is stripped on every path, so a
//!   caller can never forge the sensitivity verdict (the same trust
//!   posture as `llm_d_sc`'s provenance headers);
//! * the decision is fail-closed: a body that cannot be parsed is marked
//!   sensitive, because an unscannable request must not reach a
//!   non-approved pool. A request with no body at all has nothing to leak
//!   and is marked general;
//! * one structured JSON log line per request (target `gci_screen`), shaped
//!   like the `gci_pii_observe` lines so the agreement tooling can join it.
//!
//! The filter never selects a cluster: routing stays the router's decision,
//! keyed on `X-Demo-Step` × the screen header this filter sets.

#![deny(missing_docs)]

use async_trait::async_trait;use http::{HeaderName, HeaderValue};
use praxis_filter::{
    BodyAccess, BodyMode, FilterAction, FilterError, HttpFilter, HttpFilterContext,
    parse_filter_config,
};
use regex::Regex;
use serde::Deserialize;

/// The name this filter registers under.
pub const FILTER_NAME: &str = "gci_screen";

/// The demo screen's pattern catalog (`demo/src/hybrid_demo/pii.py`,
/// `PII_PATTERNS`), mirrored exactly — same regexes, same entity names — as
/// the `gci-pii-observe`/`gci-pii-gate` policy plugins in
/// `policy-host/src/lib.rs`, so the in-gateway decision, the policy-layer
/// scanners and the pipeline's regex screen all answer the same question.
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

/// Header the pipeline uses to name the review step; logged for the
/// agreement join, never used for the decision.
static STEP_HEADER: HeaderName = HeaderName::from_static("x-demo-step");

/// Header carrying the document session id; logged for the agreement join.
static SESSION_HEADER: HeaderName = HeaderName::from_static("x-session-id");

/// Filter settings. Defaults mirror the pipeline screen; an unknown entity
/// name fails the load — a typo must not silently narrow the screen.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GciScreenConfig {
    /// Routing header this filter owns. Default `X-Demo-Screen`.
    #[serde(default = "default_header")]
    header: String,
    /// Header value marking a sensitive request. Default `sensitive`.
    #[serde(default = "default_sensitive")]
    sensitive_value: String,
    /// Header value marking a general request. Default `general`.
    #[serde(default = "default_general")]
    general_value: String,
    /// Entity names from the catalog to scan for; `None` means all of them.
    #[serde(default)]
    detect: Option<Vec<String>>,
    /// Body bytes buffered for the scan. Default 4 MiB, the policy layer's
    /// `max_request_bytes`.
    #[serde(default = "default_max_body_bytes")]
    max_body_bytes: usize,
    /// Mark an unparseable body sensitive instead of general. Default true;
    /// the pipeline screen's fail-closed invariant, moved into the gateway.
    #[serde(default = "default_fail_closed")]
    fail_closed: bool,
}

fn default_header() -> String {
    "X-Demo-Screen".to_owned()
}
fn default_sensitive() -> String {
    "sensitive".to_owned()
}
fn default_general() -> String {
    "general".to_owned()
}
fn default_max_body_bytes() -> usize {
    4_194_304
}
fn default_fail_closed() -> bool {
    true
}

/// The outcome of one scan, before any context mutation, so every row of
/// the decision table is unit-testable without a request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Decision {
    /// The header value to set.
    pub value: String,
    /// Why: matched entities, or the failure reason.
    pub reason: Reason,
}

/// Why the decision came out the way it did.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Reason {
    /// These catalog entities matched.
    Matched(Vec<String>),
    /// Nothing matched.
    Clean,
    /// No body was buffered; there was nothing to scan.
    NoBody,
    /// The body could not be parsed as an OpenAI-shaped request.
    Unparseable,
}

impl Decision {
    /// The decision for a matched entity list.
    #[must_use]
    pub fn from_matches(matched: Vec<String>, cfg: &GciScreenConfig) -> Self {
        if matched.is_empty() {
            Self { value: cfg.general_value.clone(), reason: Reason::Clean }
        } else {
            Self { value: cfg.sensitive_value.clone(), reason: Reason::Matched(matched) }
        }
    }

    /// The decision when the body cannot be parsed, honouring `fail_closed`.
    #[must_use]
    pub fn unparseable(cfg: &GciScreenConfig) -> Self {
        if cfg.fail_closed {
            Self { value: cfg.sensitive_value.clone(), reason: Reason::Unparseable }
        } else {
            Self { value: cfg.general_value.clone(), reason: Reason::Unparseable }
        }
    }
}

/// The filter.
#[derive(Debug)]
pub struct GciScreenFilter {
    cfg: GciScreenConfig,
    patterns: Vec<(String, Regex)>,
    header: HeaderName,
}

impl GciScreenFilter {
    /// Build the filter from its YAML config; validation happens here so a
    /// bad config fails proxy startup rather than a request.
    ///
    /// # Errors
    ///
    /// Returns a [`FilterError`] on a malformed config or an unknown entity.
    pub fn from_config(config: &serde_yaml::Value) -> Result<Box<dyn HttpFilter>, FilterError> {
        Ok(Box::new(Self::new(parse_filter_config(FILTER_NAME, config)?)?))
    }

    /// Build from an already-deserialized config.
    ///
    /// # Errors
    ///
    /// Returns a [`FilterError`] when `detect` names an entity outside the
    /// catalog, or the routing header is not a legal header name.
    pub fn new(cfg: GciScreenConfig) -> Result<Self, FilterError> {
        let selected: Vec<&str> = match &cfg.detect {
            None => PATTERNS.iter().map(|(name, _)| *name).collect(),
            Some(names) => {
                for name in names {
                    if !PATTERNS.iter().any(|(n, _)| *n == name.as_str()) {
                        return Err(format!(
                            "{FILTER_NAME}: unknown entity '{name}' in detect:; the catalog is: {}",
                            PATTERNS.iter().map(|(n, _)| *n).collect::<Vec<_>>().join(", ")
                        )
                        .into());
                    }
                }
                names.iter().map(|n| n.as_str()).collect()
            }
        };
        let patterns = PATTERNS
            .iter()
            .filter(|(name, _)| selected.contains(name))
            .filter_map(|(name, re)| {
                Regex::new(re).ok().map(|compiled| ((*name).to_owned(), compiled))
            })
            .collect();
        let header = HeaderName::try_from(cfg.header.clone()).map_err(|_| {
            FilterError::from(format!("{FILTER_NAME}: '{}' is not a valid header name", cfg.header))
        })?;
        Ok(Self { cfg, patterns, header })
    }

    /// Scan every text part of an OpenAI-shaped request body.
    ///
    /// `messages[].content` as a string, or as an array of `{type: "text",
    /// text}` parts. Every matching entity name, deduplicated and sorted —
    /// all matches, not just the first, so one shared false positive is
    /// distinguishable from a real one in the logs.
    #[must_use]
    pub fn scan(&self, body: &[u8]) -> Vec<String> {
        let Ok(value) = serde_json::from_slice::<serde_json::Value>(body) else {
            return Vec::new();
        };
        let mut texts: Vec<&str> = Vec::new();
        if let Some(messages) = value.get("messages").and_then(|m| m.as_array()) {
            for message in messages {
                match message.get("content") {
                    Some(serde_json::Value::String(text)) => texts.push(text.as_str()),
                    Some(serde_json::Value::Array(parts)) => {
                        for part in parts {
                            if let Some(text) = part.get("text").and_then(|t| t.as_str()) {
                                texts.push(text);
                            }
                        }
                    },
                    _ => {},
                }
            }
        }
        let mut out: Vec<String> = Vec::new();
        for text in texts {
            for (name, re) in &self.patterns {
                if re.is_match(text) && !out.contains(name) {
                    out.push(name.clone());
                }
            }
        }
        out.sort();
        out
    }

    /// Whether the body is an OpenAI-shaped request we know how to read.
    #[must_use]
    pub fn parseable(body: &[u8]) -> bool {
        serde_json::from_slice::<serde_json::Value>(body)
            .map(|v| v.get("messages").is_some_and(|m| m.is_array()))
            .unwrap_or(false)
    }

    /// Strip a client-supplied routing header: the verdict is gateway-owned.
    fn strip_client_header(&self, ctx: &mut HttpFilterContext<'_>) {
        if ctx.request.headers.contains_key(&self.header) {
            ctx.request_headers_to_remove.push(self.header.clone());
        }
    }

    /// Apply the decision: set the header, log one line, continue.
    fn apply(&self, ctx: &mut HttpFilterContext<'_>, decision: &Decision, text_chars: usize) -> FilterAction {
        ctx.set_metadata("gci_screen.decision", decision.value.clone());
        if let Reason::Matched(matched) = &decision.reason {
            ctx.set_metadata("gci_screen.matched", matched.join(","));
        }
        let line = serde_json::json!({
            "plugin": FILTER_NAME,
            "action": decision.value,
            "session_id": header_str(ctx, &SESSION_HEADER),
            "step": header_str(ctx, &STEP_HEADER),
            "matched": match &decision.reason {
                Reason::Matched(m) => serde_json::Value::from(m.clone()),
                _ => serde_json::Value::from(Vec::<String>::new()),
            },
            "text_chars": text_chars,
            "reason": match &decision.reason {
                Reason::Matched(_) => "matched",
                Reason::Clean => "clean",
                Reason::NoBody => "no_body",
                Reason::Unparseable => "unparseable",
            },
        });
        tracing::info!(target: "gci_screen", "{line}");
        set_header(ctx, &self.header, &decision.value);
        FilterAction::Continue
    }
}

fn header_str(ctx: &HttpFilterContext<'_>, name: &HeaderName) -> String {
    ctx.request
        .headers
        .get(name)
        .and_then(|v| v.to_str().ok())
        .unwrap_or_default()
        .to_owned()
}

/// Queue a routing header, skipping values HTTP cannot carry.
fn set_header(ctx: &mut HttpFilterContext<'_>, name: &HeaderName, value: &str) {
    if let Ok(value) = HeaderValue::from_str(value) {
        ctx.request_headers_to_set.push((name.clone(), value));
    }
}

#[async_trait]
impl HttpFilter for GciScreenFilter {
    fn name(&self) -> &'static str {
        FILTER_NAME
    }

    /// This filter never selects a cluster: routing stays with the router,
    /// keyed on `X-Demo-Step` × the header this filter sets.
    fn selects_cluster(&self) -> bool {
        false
    }

    fn selected_clusters(&self) -> Vec<String> {
        Vec::new()
    }

    /// `ReadOnly` is load-bearing: `compute_body_capabilities` ignores
    /// `request_body_mode` entirely when access is `BodyAccess::None`, and
    /// `ctx.buffered_request_body` would be `None`.
    fn request_body_access(&self) -> BodyAccess {
        BodyAccess::ReadOnly
    }

    fn request_body_mode(&self) -> BodyMode {
        BodyMode::StreamBuffer { max_bytes: Some(self.cfg.max_body_bytes) }
    }

    /// The body is pre-read whole before the header phase, so all the work
    /// happens in `on_request`.
    async fn on_request_body(
        &self,
        _ctx: &mut HttpFilterContext<'_>,
        _body: &mut Option<bytes::Bytes>,
        _end_of_stream: bool,
    ) -> Result<FilterAction, FilterError> {
        Ok(FilterAction::BodyDone)
    }

    async fn on_request(&self, ctx: &mut HttpFilterContext<'_>) -> Result<FilterAction, FilterError> {
        // Step 1: always — the verdict is gateway-owned.
        self.strip_client_header(ctx);

        // Step 2: no body has nothing to leak; an unparseable body is a
        // fail-closed decision, never a 4xx — a screen must not become a
        // request validator.
        let Some(body) = ctx.buffered_request_body.clone() else {
            let decision =
                Decision { value: self.cfg.general_value.clone(), reason: Reason::NoBody };
            return Ok(self.apply(ctx, &decision, 0));
        };
        if !Self::parseable(&body) {
            let decision = Decision::unparseable(&self.cfg);
            return Ok(self.apply(ctx, &decision, body.len()));
        }

        // Step 3: scan and apply.
        let matched = self.scan(&body);
        let text_chars = count_text_chars(&body);
        let decision = Decision::from_matches(matched, &self.cfg);
        Ok(self.apply(ctx, &decision, text_chars))
    }
}

/// Total characters across the body's text parts, for the log line.
fn count_text_chars(body: &[u8]) -> usize {
    let Ok(value) = serde_json::from_slice::<serde_json::Value>(body) else {
        return 0;
    };
    let mut total = 0;
    if let Some(messages) = value.get("messages").and_then(|m| m.as_array()) {
        for message in messages {
            match message.get("content") {
                Some(serde_json::Value::String(text)) => total += text.chars().count(),
                Some(serde_json::Value::Array(parts)) => {
                    for part in parts {
                        if let Some(text) = part.get("text").and_then(|t| t.as_str()) {
                            total += text.chars().count();
                        }
                    }
                },
                _ => {},
            }
        }
    }
    total
}

praxis_filter::export_filters! {
    http "gci_screen" => GciScreenFilter::from_config,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(detect: Option<Vec<String>>, fail_closed: bool) -> GciScreenConfig {
        GciScreenConfig {
            header: default_header(),
            sensitive_value: default_sensitive(),
            general_value: default_general(),
            detect,
            max_body_bytes: default_max_body_bytes(),
            fail_closed,
        }
    }

    fn body(messages: serde_json::Value) -> Vec<u8> {
        serde_json::json!({ "model": "Semantic-Qwen", "messages": messages }).to_string().into()
    }

    #[test]
    fn clean_body_is_general() {
        let filter = GciScreenFilter::new(config(None, true)).unwrap();
        let matched = filter.scan(&body(serde_json::json!([
            {"role": "user", "content": "Quote the termination clause from this agreement."}
        ])));
        let decision = Decision::from_matches(matched, &filter.cfg);
        assert_eq!(decision.value, "general");
        assert_eq!(decision.reason, Reason::Clean);
    }

    #[test]
    fn email_in_any_message_part_is_sensitive() {
        let filter = GciScreenFilter::new(config(None, true)).unwrap();
        let matched = filter.scan(&body(serde_json::json!([
            {"role": "system", "content": "You review contracts."},
            {"role": "user", "content": "Contact john.doe@example.com about schedule 3."}
        ])));
        let decision = Decision::from_matches(matched, &filter.cfg);
        assert_eq!(decision.value, "sensitive");
        assert_eq!(decision.reason, Reason::Matched(vec!["EMAIL_ADDRESS".to_owned()]));
    }

    #[test]
    fn marker_words_are_sensitive() {
        let filter = GciScreenFilter::new(config(None, true)).unwrap();
        let matched = filter.scan(&body(serde_json::json!([
            {"role": "user", "content": "This document contains personal data (see Schedule 3)."}
        ])));
        assert!(matches!(
            Decision::from_matches(matched, &filter.cfg).reason,
            Reason::Matched(names) if names.contains(&"PERSONAL_DATA_MARKER".to_owned())
        ));
    }

    #[test]
    fn structured_content_parts_are_scanned() {
        let filter = GciScreenFilter::new(config(None, true)).unwrap();
        let matched = filter.scan(&body(serde_json::json!([
            {"role": "user", "content": [
                {"type": "text", "text": "review this"},
                {"type": "text", "text": "employee id EMP-4451 signs here"}
            ]}
        ])));
        let decision = Decision::from_matches(matched, &filter.cfg);
        assert_eq!(decision.value, "sensitive");
    }

    #[test]
    fn narrow_detect_narrows_the_scan() {
        let filter = GciScreenFilter::new(config(Some(vec!["EMAIL_ADDRESS".to_owned()]), true))
            .unwrap();
        // A marker word alone does not trip an email-only catalog...
        let matched = filter.scan(&body(serde_json::json!([
            {"role": "user", "content": "This document contains personal data."}
        ])));
        assert!(matched.is_empty());
        // ...but an email still does.
        let matched = filter.scan(&body(serde_json::json!([
            {"role": "user", "content": "Email a@example.com"}
        ])));
        assert_eq!(matched, vec!["EMAIL_ADDRESS".to_owned()]);
    }

    #[test]
    fn unknown_entity_fails_the_load() {
        let err = GciScreenFilter::new(config(Some(vec!["NOT_AN_ENTITY".to_owned()]), true));
        assert!(err.is_err());
    }

    #[test]
    fn unparseable_body_fails_closed_by_default_and_can_be_configured_open() {
        let closed = GciScreenFilter::new(config(None, true)).unwrap();
        assert_eq!(Decision::unparseable(&closed.cfg).value, "sensitive");
        let open = GciScreenFilter::new(config(None, false)).unwrap();
        assert_eq!(Decision::unparseable(&open.cfg).value, "general");
    }

    #[test]
    fn garbage_body_is_not_parseable() {
        assert!(!GciScreenFilter::parseable(b"not json at all"));
        assert!(!GciScreenFilter::parseable(br#"{"messages": "not-an-array"}"#));
        assert!(GciScreenFilter::parseable(br#"{"messages": []}"#));
    }
}
