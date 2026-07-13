from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from keel.core.types import Evidence, Provenance, Reliability, Source
from keel.runbooks.wikipedia_citation import _select_covering_sources
from keel.skills.verify import VerifyInput
from keel.tools.web import SearchHit
from keel.tools.wikitext import ref_tags_balanced


def test_search_hit_preserves_passages_for_one_source():
    hit = SearchHit(
        title="Source",
        url="https://example.org/source",
        passages=["First passage.", "Second passage."],
        retrieval_method="llm_context",
    )

    assert hit.snippet == "First passage."
    assert hit.passages == ["First passage.", "Second passage."]


def test_verify_input_accepts_legacy_excerpt_and_normalizes_passages():
    value = VerifyInput(claim="A claim", source_excerpt="Evidence")

    assert value.source_passages == ["Evidence"]


def test_verify_input_rejects_missing_source_text():
    with pytest.raises(ValidationError, match="at least one source passage"):
        VerifyInput(claim="A claim")


def test_ref_balance_ignores_self_closing_named_references():
    text = '<ref name="existing" /> cited<ref>new source</ref>'

    assert ref_tags_balanced(text)
    assert not ref_tags_balanced(text + "<ref>unclosed")


def test_covering_sources_can_combine_atomic_claim_evidence():
    provenance = Provenance(
        produced_by="test",
        at=datetime.now(timezone.utc),
        run_id="test",
        inputs_hash="test",
    )

    def evidence(claim: str, url: str) -> Evidence:
        return Evidence(
            claim=claim,
            sources=[
                Source(
                    url=url,
                    title=claim,
                    accessed=datetime.now(timezone.utc),
                    reliability=Reliability.HIGH,
                    excerpt=f"Evidence for {claim}",
                    passages=[f"Evidence for {claim}", "Additional context"],
                    retrieval_method="llm_context",
                )
            ],
            confidence=0.9,
            reasoning="direct support",
            produced=provenance,
        )

    verified = [
        evidence("The award is annual.", "https://official.example/award"),
        evidence("The award has an experience limit.", "https://news.example/report"),
    ]

    sources, selected = _select_covering_sources(
        verified,
        ["The award is annual.", "The award has an experience limit."],
    )

    assert len(sources) == 2
    assert {item.claim for item in selected} == {
        "The award is annual.",
        "The award has an experience limit.",
    }
