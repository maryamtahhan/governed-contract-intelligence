"""doc_id uniqueness, independent of the (uncommitted) CUAD tarball."""
from __future__ import annotations

from hybrid_demo.pipeline.cuad import doc_id_for


def test_titles_sharing_a_truncated_prefix_get_distinct_ids():
    """Regression: two CUAD titles that match for 48 characters collided.

    The second document overwrote the first one's text file, so the conversion
    silently dropped a contract while both manifest entries survived.
    """
    common = "A" * 60
    first = doc_id_for(common + "EX-10.1", set())
    used: set[str] = set()
    a = doc_id_for(common + "EX-10.1", used)
    b = doc_id_for(common + "EX-10.2", used)
    assert first == a
    assert a != b
    assert len(a) <= 48 and len(b) <= 48
    assert b.endswith("-2")


def test_the_same_title_twice_still_yields_two_ids():
    used: set[str] = set()
    a = doc_id_for("Repeated Contract Title", used)
    b = doc_id_for("Repeated Contract Title", used)
    assert a != b
