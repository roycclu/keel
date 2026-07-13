import pytest
from pydantic import BaseModel

from keel.core.protocols import PreconditionResult, QuerySpec, ToolResult
from keel.core.types import DecisionExplanation, Evidence, Provenance, Source
from keel.llm.client import Completion, LLMMessage, StructuredCompletion, Usage
from keel.skills.draft import CitationDraft, DraftInput, DraftSource
from keel.skills.investigate import InvestigationInput
from keel.skills.locate import ClaimExtraction, LocateInput
from keel.skills.reliability import AssessInput, ReliabilityJudgment
from keel.skills.review import ReviewBrief, ReviewInput
from keel.skills.verify import SupportJudgment, VerifyInput
from keel.tools.web import Document, FetchUrlRequest, SearchHit, WebSearchRequest, WebSearchResponse
from keel.tools.wikipedia import (
    FetchArticleRequest,
    FindCitationNeededRequest,
    FindCitationNeededResponse,
    PageHit,
    SubmitEditRequest,
    VerifyEditRequest,
    VerifyEditResponse,
)
from keel.tools.wikitext import TagHit
from keel.wikipedia.models import ArticleSnapshot, WikiCitationDraft, WikiEditPayload, WikiLocator


KEY_BOUNDARY_MODELS: tuple[type[BaseModel], ...] = (
    LocateInput,
    ClaimExtraction,
    VerifyInput,
    SupportJudgment,
    DraftSource,
    DraftInput,
    CitationDraft,
    AssessInput,
    ReliabilityJudgment,
    ReviewInput,
    ReviewBrief,
    InvestigationInput,
    DecisionExplanation,
    WebSearchRequest,
    WebSearchResponse,
    SearchHit,
    FetchUrlRequest,
    Document,
    PageHit,
    FindCitationNeededRequest,
    FindCitationNeededResponse,
    FetchArticleRequest,
    SubmitEditRequest,
    VerifyEditRequest,
    VerifyEditResponse,
    TagHit,
    WikiLocator,
    ArticleSnapshot,
    WikiCitationDraft,
    WikiEditPayload,
    Provenance,
    Source,
    Evidence,
    ToolResult,
    PreconditionResult,
    QuerySpec,
    LLMMessage,
    Usage,
    Completion,
    StructuredCompletion,
)


@pytest.mark.parametrize("model", KEY_BOUNDARY_MODELS, ids=lambda model: model.__name__)
def test_key_boundary_fields_have_schema_descriptions(model: type[BaseModel]) -> None:
    missing = [name for name, field in model.model_fields.items() if not field.description]

    assert missing == []


def test_skill_field_description_is_emitted_in_json_schema() -> None:
    schema = SupportJudgment.model_json_schema()

    assert schema["properties"]["confidence"]["description"].startswith("Probability")
