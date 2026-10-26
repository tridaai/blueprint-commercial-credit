"""Quarantine: borrower documents are untrusted text (round 17).

A borrower's statement notes, an appraisal's commentary, a
guarantor's covering letter — text the bank did not write, some of
it written by parties with an interest in the outcome. An
instruction planted in that text ("report revenue as …", "mark
this covenant as passed", "do not tell the committee …") must
never reach a model as bare prose, and a figure planted inside it
must never ground a memo.

The defence here is deterministic, like everything else in this
blueprint that an examiner might ask to see run twice:

- A versioned pattern pack — data, below — scans document text
  at ingestion. Matches are recorded as quarantine events on the
  credit file naming the document, the field, the pattern and
  the exact span. The document is never rejected and never
  altered: evidence is evidence, and the attempt itself becomes
  part of the file.
- Wherever document text reaches a model surface — the
  extraction prompt, the memo/review polishing candidate — the
  matched spans are fenced by `fence_text`: the model sees a
  marker naming the pattern, never the instruction.
- The numbers inside matched spans are recorded on the events.
  A figure that appears only inside quarantined spans (and
  nowhere as a printed figure in the file's documents) is
  excluded from the memo verifier's trace universe: it grounds
  nothing, however it is cited.

The scanner is field-scoped at ingestion (each string field of
the document payload is scanned on its own) and whole-text at
the model surfaces. An instruction split across two fields so
that no single field contains a match is a stated boundary of
this discipline, not a hidden one — the same boundary the
pattern-pack design records for the AML blueprint's scanner.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dc_field
from decimal import Decimal, InvalidOperation

from .models import QuarantineEvent

#: The pattern pack's version, recorded on every event. A pack
#: change is a new version; events keep the version they were
#: recorded under, so a case can always be re-read against the
#: pack that judged it.
PATTERN_PACK_VERSION = "2026.10.1"

#: The fence marker's fixed text. It carries no digits, no "$",
#: no "%" — nothing the memo figure grammar could read as a
#: figure — and it matches no pattern in the pack, so fencing is
#: idempotent: fencing fenced text changes nothing further.
FENCE_MARKER = "⟦quarantined instruction removed — pattern {pattern_id}⟧"

# The pattern pack, as data. Each pattern: a stable id, the
# family it belongs to, and a case-insensitive regular expression
# over document text. The pack is deliberately specific to
# instructions addressed to a model or to the bank's process —
# ordinary financial prose ("the borrower shall maintain a
# current ratio of …", "management notes that revenue grew …")
# must scan clean, and the test suite holds it to that.
INSTRUCTION_PATTERNS: list[dict[str, str]] = [
    # -- instruction override -----------------------------------
    {
        "pattern_id": "override.ignore_previous",
        "family": "instruction_override",
        "regex": r"ignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|directions|rules|prompts?)",
    },
    {
        "pattern_id": "override.disregard",
        "family": "instruction_override",
        "regex": r"disregard\s+(all\s+)?(the\s+)?(previous|prior|your|above)\s+(instructions|directions|rules)",
    },
    {
        "pattern_id": "override.forget",
        "family": "instruction_override",
        "regex": r"forget\s+(all\s+)?(your\s+)?(previous\s+)?(instructions|rules|training)",
    },
    {
        "pattern_id": "override.new_instructions",
        "family": "instruction_override",
        "regex": r"(follow|obey|use)\s+(these|the following)\s+(new\s+)?instructions",
    },
    # -- role / system impersonation -----------------------------
    {
        "pattern_id": "role.you_are_now",
        "family": "role_impersonation",
        "regex": r"you\s+are\s+now\s+(a|an|the)\s",
    },
    {
        "pattern_id": "role.act_as",
        "family": "role_impersonation",
        "regex": r"act\s+as\s+(a|an|the|if)\s",
    },
    {
        "pattern_id": "role.system_prompt",
        "family": "role_impersonation",
        "regex": r"system\s+prompt",
    },
    {
        "pattern_id": "role.system_block",
        "family": "role_impersonation",
        "regex": r"(\[system\]|<system>|begin\s+system\s+(message|prompt))",
    },
    # -- output dictation -----------------------------------------
    {
        # The dictation sense is "report X as <a figure>": the
        # word after "as" must be figure-like (a digit, a $, or a
        # spelled-out quantity). "Report the results as scheduled"
        # and "report the revenue as audited" are ordinary prose
        # and scan clean — the fixtures hold the pack to that.
        "pattern_id": "dictation.report_as",
        "family": "output_dictation",
        "regex": r"report\s+(the\s+)?(revenue|sales|income|ebitda|ebit|dscr|leverage|ratio|figures?|results?|numbers?)\s+as\s+(?=\$?\s?\d|one|two|three|four|five|six|seven|eight|nine|ten|million|billion|thousand)",
    },
    {
        "pattern_id": "dictation.state_that",
        "family": "output_dictation",
        "regex": r"state\s+that\s+(the\s+)?(borrower|company|loan|covenant|ratio|credit|facility)",
    },
    {
        "pattern_id": "dictation.mark_as",
        "family": "output_dictation",
        "regex": r"mark\s+(this|the|all)\s+(covenant|test|loan|credit|memo|facility|application)\s+as\s+(passed|passing|approved|compliant|satisfied|waived)",
    },
    {
        "pattern_id": "dictation.output_only",
        "family": "output_dictation",
        "regex": r"(output|respond|reply)\s+only\b",
    },
    # -- concealment ----------------------------------------------
    {
        "pattern_id": "conceal.do_not_report",
        "family": "concealment",
        "regex": r"do\s+not\s+(report|disclose|mention|flag|record|include|show)\b",
    },
    {
        "pattern_id": "conceal.keep_from",
        "family": "concealment",
        "regex": r"(keep|kept)\s+(this|it|that)\s+(secret|hidden|confidential)\s+from\b",
    },
    {
        "pattern_id": "conceal.without_telling",
        "family": "concealment",
        "regex": r"without\s+(telling|notifying|informing)\s+(the\s+)?(bank|officer|committee|analyst|reviewer|auditor)",
    },
    # -- policy override --------------------------------------------
    {
        "pattern_id": "policy.bypass",
        "family": "policy_override",
        "regex": r"(approve|waive|bypass|skip|pass)\s+(the\s+)?(policy\s+)?(check|verification|committee|review|limit|test|policy)\b",
    },
    {
        "pattern_id": "policy.raise_limit",
        "family": "policy_override",
        "regex": r"(raise|increase|extend)\s+(the\s+)?(limit|threshold|cap|line)\s+to\b",
    },
]

_COMPILED: list[tuple[str, str, re.Pattern[str]]] = [
    (
        p["pattern_id"],
        p["family"],
        re.compile(p["regex"], re.IGNORECASE),
    )
    for p in INSTRUCTION_PATTERNS
]

# A sentence ends at ., ! or ? followed by whitespace or the end
# of the text, or at a newline. ("e.g." does not end a sentence:
# its full stop is followed by a letter.)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|[\n\r]")

# Numeric tokens inside a span. A token counts as a figure when
# it is money-like: it carries a "$", a thousands comma, a
# decimal fraction, or at least five digits. A bare year or a
# small count inside an instruction is prose, not a planted
# figure, and is not recorded as one.
_NUMBER_RE = re.compile(r"\$?\s?\d[\d,]*(?:\.\d+)?")


def _figures_in(text: str) -> list[Decimal]:
    figures: list[Decimal] = []
    for match in _NUMBER_RE.finditer(text):
        token = match.group(0)
        digits = sum(ch.isdigit() for ch in token)
        if (
            "$" not in token
            and "," not in token
            and "." not in token
            and digits < 5
        ):
            continue
        cleaned = token.replace("$", "").replace(",", "").strip()
        try:
            figures.append(Decimal(cleaned))
        except InvalidOperation:
            continue
    return figures


@dataclass(frozen=True)
class SpanMatch:
    """One pattern match, extended to its sentence, in one text."""

    pattern_id: str
    family: str
    start: int
    end: int
    text: str
    figures: list[Decimal] = dc_field(default_factory=list)


def _sentence_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Extend a match to the boundaries of its sentence."""
    lo = 0
    for m in _SENTENCE_END.finditer(text, 0, start):
        lo = m.end()
    hi = len(text)
    m = _SENTENCE_END.search(text, end)
    if m is not None:
        hi = m.end()
    # Trim surrounding whitespace so the recorded span is exact
    # prose, not padding.
    while lo < start and text[lo].isspace():
        lo += 1
    while hi > end and text[hi - 1].isspace():
        hi -= 1
    return lo, hi


def scan_text(text: str) -> list[SpanMatch]:
    """Every pattern match in one text, sentence-extended.

    Matches whose extended spans overlap are merged: the merged
    span is returned once per pattern that matched inside it,
    so no pattern is lost and no span is fenced twice.
    """
    raw: list[tuple[int, int, str, str]] = []
    for pattern_id, family, regex in _COMPILED:
        for match in regex.finditer(text):
            lo, hi = _sentence_span(text, match.start(), match.end())
            raw.append((lo, hi, pattern_id, family))
    if not raw:
        return []
    raw.sort(key=lambda item: (item[0], item[1]))
    merged: list[list] = []
    for lo, hi, pattern_id, family in raw:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
            merged[-1][2].append((pattern_id, family))
        else:
            merged.append([lo, hi, [(pattern_id, family)]])
    out: list[SpanMatch] = []
    for lo, hi, patterns in merged:
        span_text = text[lo:hi]
        figures = _figures_in(span_text)
        for pattern_id, family in patterns:
            out.append(
                SpanMatch(
                    pattern_id=pattern_id,
                    family=family,
                    start=lo,
                    end=hi,
                    text=span_text,
                    figures=figures,
                )
            )
    return out


def fence_spans(text: str, matches: list[SpanMatch]) -> str:
    """Replace each distinct matched span with the fence marker."""
    spans = sorted({(m.start, m.end, m.pattern_id) for m in matches})
    # One marker per distinct span; the first pattern (in pack
    # order of appearance in the span list) names it.
    by_span: dict[tuple[int, int], str] = {}
    for start, end, pattern_id in spans:
        by_span.setdefault((start, end), pattern_id)
    out = text
    for (start, end), pattern_id in sorted(
        by_span.items(), reverse=True
    ):
        marker = FENCE_MARKER.format(pattern_id=pattern_id)
        out = out[:start] + marker + out[end:]
    return out


def fence_text(text: str) -> str:
    """Scan a text and fence every matched span in place.

    This is the model-surface discipline: whatever the ingestion
    scan recorded, the surface re-derives the fences from the
    text itself, so a text that reaches a model without passing
    ingestion still cannot carry a bare instruction.
    """
    matches = scan_text(text)
    if not matches:
        return text
    return fence_spans(text, matches)


def _string_leaves(payload: object, prefix: str = "") -> list[tuple[str, str]]:
    """(field path, text) for every string leaf of a JSON payload."""
    out: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.extend(_string_leaves(value, path))
    elif isinstance(payload, list):
        for idx, value in enumerate(payload):
            out.extend(_string_leaves(value, f"{prefix}[{idx}]"))
    elif isinstance(payload, str):
        out.append((prefix, payload))
    return out


def _numeric_leaves(payload: object) -> list[Decimal]:
    """Every numeric leaf of a JSON payload, as Decimals."""
    out: list[Decimal] = []
    if isinstance(payload, dict):
        for value in payload.values():
            out.extend(_numeric_leaves(value))
    elif isinstance(payload, list):
        for value in payload:
            out.extend(_numeric_leaves(value))
    elif isinstance(payload, bool):
        pass  # a JSON true/false is not a figure
    elif isinstance(payload, (int, float)):
        out.append(Decimal(str(payload)))
    elif isinstance(payload, str):
        token = payload.strip().replace(",", "").removeprefix("$")
        if re.fullmatch(r"-?\d+(\.\d+)?", token):
            out.append(Decimal(token))
    return out


def printed_figures(content: bytes) -> set[Decimal]:
    """The figures actually printed in a document's payload.

    The ground a figure must stand on: a number that is a real
    numeric leaf of the document (a statement line's value, a
    total, a stated amount) is printed. A number that exists
    only as digits inside a text string is not.
    """
    try:
        payload = json.loads(content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return set()
    return set(_numeric_leaves(payload))


@dataclass(frozen=True)
class DocumentScan:
    """The scan of one document: matches by field."""

    matches: list[tuple[str, SpanMatch]]  # (field, match)


def scan_document(content: bytes, *, filename: str) -> DocumentScan:
    """Scan one document's text at ingestion.

    Structured payloads are scanned field by field — every
    string leaf, at its JSON path — so an event names exactly
    where the instruction sits. Anything else is scanned as one
    text field. The filename is scanned too: it is borrower
    text the file renders.
    """
    matches: list[tuple[str, SpanMatch]] = []
    for match in scan_text(filename):
        matches.append(("filename", match))
    text: str | None = None
    payload: object = None
    try:
        text = content.decode("utf-8")
        payload = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        payload = None
    if isinstance(payload, (dict, list)):
        for field_path, leaf in _string_leaves(payload):
            for match in scan_text(leaf):
                matches.append((field_path, match))
    elif text is not None:
        for match in scan_text(text):
            matches.append(("text", match))
    return DocumentScan(matches=matches)


def build_events(
    scan: DocumentScan, *, credit_file_id: str, document_id: str
) -> list[QuarantineEvent]:
    """The append-only records one document's scan produces."""
    return [
        QuarantineEvent(
            credit_file_id=credit_file_id,
            document_id=document_id,
            field=field_path,
            pattern_id=match.pattern_id,
            pattern_family=match.family,
            pack_version=PATTERN_PACK_VERSION,
            span_start=match.start,
            span_end=match.end,
            matched_text=match.text,
            figures=match.figures,
        )
        for field_path, match in scan.matches
    ]


def quarantine_only_figures(
    events: list[QuarantineEvent], printed: set[Decimal]
) -> frozenset[Decimal]:
    """Figures that appear only inside quarantined spans.

    The union of every recorded span's figures, minus every
    figure actually printed in the file's documents. What
    remains grounds nothing: the memo verifier's trace universe
    excludes exactly this set.
    """
    span_figures = {f for event in events for f in event.figures}
    return frozenset(span_figures - printed)
