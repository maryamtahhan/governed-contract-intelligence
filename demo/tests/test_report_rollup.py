from __future__ import annotations

from hybrid_demo.pipeline.review import report_rollup


def test_report_rollup_counts_the_pipeline_rows_not_the_prompt():
    rows = [
        {
            "ref": "C-2",
            "risk": "high",
            "governing_law": "the laws of the State of New York",
            "data_protection": True,
        },
        {
            "ref": "C-1",
            "risk": "low",
            "governing_law": "the laws of Ireland",
            "data_protection": False,
        },
        {
            "ref": "C-3",
            "risk": "high",
            "governing_law": "",
            "data_protection": False,
        },
    ]

    rollup = report_rollup(rows, has_data_protection=True)

    assert rollup["documents"] == 3
    assert rollup["risk_counts"] == {"low": 1, "medium": 0, "high": 2}
    assert rollup["governing_law_counts"] == {
        "new york": 1,
        "ireland": 1,
        "missing": 1,
    }
    assert rollup["highest_risk_ref"] == "C-2"
    assert rollup["missing_data_protection"] == 2


def test_report_rollup_omits_data_protection_when_the_dataset_has_no_category():
    rollup = report_rollup(
        [{"ref": "C-1", "risk": "low", "governing_law": "Ireland",
          "data_protection": False}],
        has_data_protection=False,
    )

    assert "missing_data_protection" not in rollup
