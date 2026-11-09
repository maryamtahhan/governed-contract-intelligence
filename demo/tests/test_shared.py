"""The routing sets and risk rubric have one definition, not one per caller."""
from __future__ import annotations

from hybrid_demo import routes
from hybrid_demo.evalkit import score
from hybrid_demo.pipeline import cuad, generate, review, risk
from hybrid_demo.specialist import questions


def test_one_routing_definition():
    assert score.APPROVED_ROUTES is routes.APPROVED_ROUTES
    assert score.GPU_ROUTES is routes.GPU_ROUTES
    assert review.APPROVED_ROUTES is routes.APPROVED_ROUTES
    assert review.GPU_ROUTES is routes.GPU_ROUTES
    assert routes.GPU_ROUTES.isdisjoint(routes.CPU_ROUTES)
    assert routes.APPROVED_ROUTES <= (routes.GPU_ROUTES | routes.CPU_ROUTES)


def test_rubric_text_matches_the_annotated_fields():
    with_dp = risk.rubric_text(True)
    without_dp = risk.rubric_text(False)
    assert "data-protection" in with_dp
    assert "data-protection" not in without_dp
    assert "uncapped" in with_dp and "material breach" in without_dp


def test_assess_risk_rules():
    clauses = {"termination": "Either party may terminate on notice",
               "liability_cap": "EUR 1,000,000",
               "data_protection": "DPA in force"}
    assert risk.assess_risk(clauses, has_data_protection=True) == "low"
    # A dataset without a data-protection field must not be judged on it.
    assert risk.assess_risk({**clauses, "data_protection": None},
                            has_data_protection=False) == "low"
    assert risk.assess_risk({**clauses, "data_protection": None},
                            has_data_protection=True) == "high"
    assert risk.assess_risk({**clauses, "liability_cap": "unlimited"},
                            has_data_protection=True) == "high"
    assert risk.assess_risk({**clauses, "liability_cap": None},
                            has_data_protection=False) == "high"
    assert risk.assess_risk({**clauses, "termination": "material breach only"},
                            has_data_protection=False) == "medium"
    assert risk.assess_risk(clauses, has_data_protection=False, uncapped=True) == "high"


def test_generator_and_cuad_use_the_shared_rubric():
    clauses = {"termination": "Either party may terminate", "governing_law": "Ireland",
               "liability_cap": "EUR 1,000,000", "audit_rights": "annual audit",
               "data_protection": None}
    assert generate.risk_of(clauses) == "high"          # DP is annotated and absent
    assert cuad.risk_of(clauses, False) == "low"         # CUAD never asks about DP


def test_unsupported_clause_has_no_question_and_no_alias():
    assert questions.question_for("data protection") is None
    assert questions.question_for("data_protection") is None
    assert "data protection" not in questions.CLAUSE_ALIASES
    assert questions.question_for("audit_rights") == questions.CUAD_QUESTIONS["audit rights"]


def test_extraction_prompt_maps_back_to_a_cuad_question():
    body = review.extract_body("Some contract text", "audit_rights")
    assert questions.clause_from_prompt(body["messages"]) == "audit rights"
    assert questions.question_for(questions.clause_from_prompt(body["messages"]))


def test_cause_question_is_a_narrow_termination_fallback():
    cause = "This Agreement may be terminated only for material breach that remains uncured."
    convenience = "Either party may terminate this Agreement upon thirty days' notice."
    assert questions.cause_question_for("termination", cause) == (
        questions.CAUSE_TERMINATION_QUESTION)
    assert questions.cause_question_for("termination", convenience) is None
    # A generic breach in a warranty clause must not trigger the cause question.
    assert questions.cause_question_for(
        "termination", "The Supplier warrants no breach of any representation.") is None
    assert questions.cause_question_for("governing_law", cause) is None
