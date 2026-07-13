"""Pure wikitext transforms. No I/O, no LLM, no context — just deterministic text.

These are plain functions, not `Tool` implementers, because they cannot fail in a way
the runbook loop must classify. They are the deterministic core of the submit path:
the drafting model proposes a citation, and these functions turn it into an exact,
auditable diff.
"""

from __future__ import annotations

import difflib
import re

from pydantic import BaseModel, Field

from keel.core.types import Source

# Template names that mean "this needs a citation", case-insensitive, spaces/underscores.
_CN_ALIASES = ("citation needed", "cn", "fact", "citation-needed")
_CN_RE = re.compile(
    r"\{\{\s*(?:" + "|".join(a.replace(" ", r"[ _]") for a in _CN_ALIASES) + r")\b[^{}]*\}\}",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^\s*(={2,6})\s*(.+?)\s*\1\s*$", re.MULTILINE)


def format_citation(source: Source, *, style: str = "cs1") -> str:
    """Render a Source as a CS1 <ref>...</ref> block.

    CS1 is the {{cite web}} family, English Wikipedia's default. `style` is a hook for
    other citation styles later (AGENTS.md #1); only "cs1" is implemented in Phase 1.
    """
    if style != "cs1":
        raise ValueError(f"unsupported citation style: {style!r}")

    def esc(value: str) -> str:
        # A literal pipe would terminate a template parameter; {{!}} is the wiki escape.
        return value.replace("|", "{{!}}").strip()

    fields = [f"url={source.url}", f"title={esc(source.title)}"]
    if source.publisher:
        fields.append(f"publisher={esc(source.publisher)}")
    if source.published:
        fields.append(f"date={source.published.isoformat()}")
    fields.append(f"access-date={source.accessed.date().isoformat()}")
    return "<ref>{{cite web |" + " |".join(fields) + "}}</ref>"


def render_diff(before: str, after: str, *, filename: str = "article") -> str:
    """Unified diff of two wikitext blobs, for the human review brief."""
    lines = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        lineterm="",
    )
    return "\n".join(lines)


class TagHit(BaseModel):
    """A deterministically located [citation needed] tag. No LLM involved."""

    markup: str  # exact template text, used later by replace_nth
    occurrence: int = Field(ge=0)  # 0-based index among identical markups on the page
    section: str | None  # nearest preceding heading
    window: str  # surrounding wikitext (claim usually precedes the tag)


def find_citation_needed_tags(wikitext: str, *, window: int = 400) -> list[TagHit]:
    """Locate every citation-needed template. Deterministic guardrail for the locate
    skill: positions come from here, never from the model, so the model cannot point
    the pipeline at a tag that does not exist."""
    seen: dict[str, int] = {}
    hits: list[TagHit] = []
    headings = [(m.start(), m.group(2)) for m in _HEADING_RE.finditer(wikitext)]
    for m in _CN_RE.finditer(wikitext):
        markup = m.group(0)
        occ = seen.get(markup, 0)
        seen[markup] = occ + 1
        section = None
        for pos, title in headings:
            if pos < m.start():
                section = title
            else:
                break
        start = max(0, m.start() - window)
        hits.append(
            TagHit(
                markup=markup,
                occurrence=occ,
                section=section,
                window=wikitext[start : m.end() + window // 4],
            )
        )
    return hits


def replace_nth(text: str, needle: str, replacement: str, n: int) -> str:
    """Replace the n-th (0-based) occurrence of `needle`. Raises ValueError if absent.

    The ValueError is the signal that the article changed under us (the tag we located
    at draft time is gone); the runbook converts it to an ABANDONED transition.
    """
    start = -1
    for _ in range(n + 1):
        start = text.find(needle, start + 1)
        if start == -1:
            raise ValueError(f"occurrence {n} of {needle!r} not found")
    return text[:start] + replacement + text[start + len(needle) :]
