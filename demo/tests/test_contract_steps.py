from __future__ import annotations

from hybrid_demo.evalkit.contract_steps import (
    CONTROL_DOC,
    LABELS,
    LONG_DOC,
    UNROUTED,
    Prediction,
    anchor_leakage,
    build_cases,
    case_prompt,
    format_summary,
    load_taxonomy,
    prediction_from_headers,
    request_body,
    score_predictions,
    validate_taxonomy,
)


def test_contract_step_taxonomy_is_valid():
    taxonomy = load_taxonomy()
    assert taxonomy["classifier_id"] == "contract-step"
    assert taxonomy["signal"] == "contract_step"
    assert validate_taxonomy(taxonomy) == []
    assert set(taxonomy["labels"]) == set(LABELS)


def test_case_set_covers_real_paraphrase_control_and_long_families():
    cases = build_cases()
    families = {case.family for case in cases}
    assert families == {
        "real-prompt", "paraphrase", "same-document-control", "long-document",
        "agent-shaped",
    }
    assert {case.expected for case in cases} == set(LABELS) | {UNROUTED}

    real = [case for case in cases if case.family == "real-prompt"]
    assert {case.expected for case in real} == set(LABELS)
    assert all(case.note for case in real)

    paraphrases = [case for case in cases if case.family == "paraphrase"]
    assert len(paraphrases) == 16
    for label in LABELS:
        assert sum(1 for case in paraphrases if case.expected == label) == 4


def test_same_document_control_holds_the_body_constant():
    controls = [case for case in build_cases()
                if case.family == "same-document-control"]
    assert {case.expected for case in controls} == set(LABELS)
    assert all(CONTROL_DOC in case_prompt(case) for case in controls)


def test_long_document_family_holds_one_over_budget_body_constant():
    long_cases = [case for case in build_cases()
                  if case.family == "long-document"]
    assert {case.expected for case in long_cases} == set(LABELS)
    assert all(case.long_document for case in long_cases)
    assert all(LONG_DOC in case_prompt(case) for case in long_cases)
    assert all(len(case_prompt(case)) > 4096 for case in long_cases)
    assert all(case_prompt(case).startswith((
        "Tell me what kind of agreement",
        "Find the termination language",
        "Assess this contract's legal exposure",
        "Summarize this document set",
    )) for case in long_cases)


def test_agent_shaped_cases_skip_no_user_and_use_the_last_user_turn():
    cases = [case for case in build_cases() if case.family == "agent-shaped"]
    assert len(cases) == 5

    no_user = [case for case in cases if case.expected == UNROUTED]
    assert {case.case_id for case in no_user} == {
        "agent-tool-result-only", "agent-assistant-only", "agent-system-only",
    }
    assert all(case_prompt(case) == "" for case in no_user)
    assert any(
        any(message.get("role") == "tool" for message in case.body["messages"])
        for case in no_user
    )
    assert any(
        any(message.get("role") == "assistant" for message in case.body["messages"])
        for case in no_user
    )

    with_user = [case for case in cases if case.expected != UNROUTED]
    assert {case.case_id: case.expected for case in with_user} == {
        "agent-tool-then-user-risk": "RISK",
        "agent-assistant-then-user-extract": "EXTRACT",
    }
    assert all(case_prompt(case).startswith(("Assess", "Quote"))
               for case in with_user)


def test_taxonomy_anchors_are_not_copied_from_the_cases():
    # This is the guard against a near-tautological evaluation: if an anchor
    # contains a whole test prompt (or vice versa), the score says nothing about
    # generalisation.
    assert anchor_leakage(build_cases(), load_taxonomy()) == []


def test_real_prompts_are_the_pipeline_builders():
    from hybrid_demo.pipeline.review import classify_body

    real = next(case for case in build_cases()
                if case.case_id == "real-classify")
    assert real.body == classify_body(CONTROL_DOC)
    assert case_prompt(real).startswith("Classify the following contract by type")


def test_request_body_drops_schema_and_caps_generation():
    case = next(case for case in build_cases() if case.case_id == "real-report")
    body = request_body(case, max_tokens=4)
    assert body["max_tokens"] == 4
    assert body["temperature"] == 0
    assert "response_format" not in body
    assert "guided_json" not in body
    assert "structured_outputs" not in body
    assert body["messages"] == case.body["messages"]


def test_prediction_reads_filter_and_relay_headers():
    direct = prediction_from_headers("c1", {
        "x-llm-d-sc-label": "risk",
        "x-llm-d-sc-score": "0.81",
        "x-llm-d-sc-status": "OK",
    })
    assert direct.label == "RISK"
    assert direct.score == 0.81
    assert direct.status == "OK"

    relayed = prediction_from_headers("c2", {
        "x-demo-classification": "REPORT",
        "x-demo-route": "vllm-gpu",
    })
    assert relayed.label == "REPORT"
    assert relayed.route == "vllm-gpu"


def test_scoring_separates_control_from_overall_accuracy():
    cases = build_cases()
    controls = {"same-document-control", "long-document"}
    predictions = [
        Prediction(case.case_id,
                   None if case.expected == UNROUTED
                   else case.expected if case.family in controls
                   else "CLASSIFY" if case.expected != "CLASSIFY" else "MISSING",
                   None, None)
        for case in cases
    ]
    result = score_predictions(cases, predictions)
    assert result["same_document_control"]["correct"] == 4
    assert result["same_document_control"]["total"] == 4
    assert result["long_document_control"]["correct"] == 4
    assert result["long_document_control"]["total"] == 4
    assert result["agent_no_user"]["correct"] == 3
    assert result["agent_no_user"]["total"] == 3
    assert result["accuracy"] < 1.0
    assert result["confusion"]["REPORT"]["CLASSIFY"] == 5
    assert "long-document control" in format_summary(result)
    assert "no-user turns" in format_summary(result)


def test_check_mode_runs_without_a_gateway(capsys):
    from hybrid_demo.evalkit.contract_steps import main

    main(["--check"])
    payload = capsys.readouterr().out
    assert '"cases": 33' in payload
    assert '"anchor_leakage": []' in payload
    assert '"prompts_over_filter_budget": []' in payload
    assert '"long_document_cases"' in payload
