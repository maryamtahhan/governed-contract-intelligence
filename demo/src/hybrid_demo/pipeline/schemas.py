"""JSON schemas and structured-output plumbing for the pipeline steps.

The pipeline used to rely on the model returning JSON in prose, then parsed
it leniently.  vLLM can constrain decoding to a schema, which removes the
malformed-JSON failure mode entirely.  The field name matters across versions:

* current vLLM: ``{"structured_outputs": {"json": <schema>}}``
* older vLLM:   ``{"guided_json": <schema>}``
* OpenAI-compatible clients: ``{"response_format": {"type": "json_schema", ...}}``

``HYBRID_STRUCTURED_OUTPUTS`` selects the mode:

    unset / 0 / off   -> do nothing (measured default; byte-identical bodies)
    1 / vllm          -> current vLLM structured_outputs
    legacy            -> guided_json
    openai            -> response_format

The schemas are intentionally permissive about extra keys: the scoring code
and the fake pool should still see an honest answer, and a constrained
decoder that forbids extra keys can change model quality.  Measure the A/B
before enabling this for a quoted run.
"""
from __future__ import annotations

import os
from typing import Any

CLAUSE_ENUM = ["termination", "governing_law", "liability_cap", "data_protection",
               "confidentiality", "indemnification", "assignment", "renewal_term",
               "notice_period", "payment_terms"]

CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["MSA", "NDA", "SOW", "DPA", "UNKNOWN"]},
        "initial_term_months": {"type": "integer"},
        "ref": {"type": "string"},
    },
    "required": ["type", "initial_term_months", "ref"],
}

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "clause": {"type": "string"},
        "found": {"type": "boolean"},
        "text": {"type": ["string", "null"]},
        "ref": {"type": "string"},
    },
    "required": ["clause", "found", "text", "ref"],
}

RISK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "ref": {"type": "string"},
    },
    "required": ["risk"],
}

REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "documents": {"type": "integer"},
        "risk_counts": {
            "type": "object",
            "properties": {
                "low": {"type": "integer"},
                "medium": {"type": "integer"},
                "high": {"type": "integer"},
            },
            "required": ["low", "medium", "high"],
        },
        "governing_law_counts": {
            "type": "object",
            "additionalProperties": {"type": "integer"},
        },
        "missing_data_protection": {"type": "integer"},
        "highest_risk_ref": {"type": ["string", "null"]},
        "ref": {"type": "string"},
    },
    "required": ["documents", "risk_counts", "governing_law_counts",
                 "highest_risk_ref"],
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "classify": CLASSIFY_SCHEMA,
    "extract": EXTRACT_SCHEMA,
    "risk": RISK_SCHEMA,
    "report": REPORT_SCHEMA,
}


def mode() -> str:
    value = (os.environ.get("HYBRID_STRUCTURED_OUTPUTS") or "").strip().lower()
    if value in ("", "0", "off", "none", "false"):
        return "off"
    if value in ("1", "vllm", "structured", "structured_outputs"):
        return "vllm"
    if value in ("legacy", "guided_json"):
        return "legacy"
    if value in ("openai", "response_format"):
        return "openai"
    raise ValueError(f"unknown HYBRID_STRUCTURED_OUTPUTS value {value!r}")


def schema_fields(schema: dict[str, Any], step: str) -> dict[str, Any]:
    """The request fragment for one schema, in the selected wire format."""
    selected = mode()
    if selected == "off":
        return {}
    if selected == "vllm":
        return {"structured_outputs": {"json": schema}}
    if selected == "legacy":
        return {"guided_json": schema}
    return {
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": f"{step}_answer", "schema": schema,
                            "strict": False},
        }
    }


def apply(body: dict[str, Any], step: str) -> dict[str, Any]:
    """Return ``body`` with the schema fields for ``step`` when enabled.

    Mutates a shallow copy so callers can keep building their own dict.
    """
    schema = SCHEMAS.get(step)
    if schema is None:
        return body
    fields = schema_fields(schema, step)
    if not fields:
        return body
    return {**body, **fields}
