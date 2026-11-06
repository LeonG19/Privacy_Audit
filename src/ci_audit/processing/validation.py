"""Parsing and validating model output. Nothing here trusts the model: evidence must exist in the source."""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Optional

from pydantic import BaseModel, ValidationError

from ci_audit.schemas import CI_PARAMS, FinalCIParameter, LLMExtraction, SourceRecord


class ResponseParseError(ValueError):
    pass


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_response(text: str) -> dict:
    """Extract a JSON object from model text (tolerates reasoning blocks and code fences)."""
    if not text or not text.strip():
        raise ResponseParseError("empty response")
    s = _THINK.sub("", text).strip()
    s = _FENCE.sub("", s).strip()
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        start = s.find("{")
        if start < 0:
            raise ResponseParseError("no JSON object found in response") from None
        try:
            obj, _ = json.JSONDecoder().raw_decode(s[start:])
        except json.JSONDecodeError as e:
            raise ResponseParseError(f"invalid JSON: {e.msg} at char {e.pos}") from None
    if not isinstance(obj, dict):
        raise ResponseParseError(f"top-level JSON must be an object, got {type(obj).__name__}")
    return obj


def validate_model(raw: dict, model: type[BaseModel]):
    """-> (instance, [error strings]); error strings are short enough to feed back to the model."""
    try:
        return model.model_validate(raw), []
    except ValidationError as e:
        errs = [f"{'.'.join(str(x) for x in er['loc'])}: {er['msg']}" for er in e.errors()[:12]]
        return None, errs


# --------------------------------------------------------------------------- evidence checking
_QUOTES = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "}


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    for a, b in _QUOTES.items():
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip().lower()


def evidence_in_source(span: Optional[str], source_text: str) -> bool:
    """True if `span` occurs in the source (case/space/quote-insensitive). '...' marks elisions between pieces."""
    if not span or not span.strip():
        return False
    src = normalize(source_text)
    pieces = [p for p in re.split(r"\.\.\.|…", span) if p.strip()]
    return bool(pieces) and all(normalize(p).strip(" .,;") in src for p in pieces)


def check_extraction(ex: LLMExtraction, source_text: str) -> tuple[LLMExtraction, list[str]]:
    """Return (possibly pruned extraction, warnings).

    * CI values: kept, but flagged when null without a reason, non-null without evidence, or evidence not in the source.
    * Claims of permission / restriction / relationship whose evidence is not in the source are DROPPED (the model may
      not invent consent, restrictions or relationships); the dropped text is named in the warning.
    """
    w: list[str] = []
    for name in CI_PARAMS:
        p = getattr(ex.ci_parameters, name)
        if p.value is None:
            if not (p.missing_reason or "").strip():
                w.append(f"ci.{name}: null without missing_reason")
            if p.evidence:
                w.append(f"ci.{name}: evidence given for a null value")
        else:
            if not p.evidence:
                w.append(f"ci.{name}: value without evidence")
            for span in p.evidence:
                if not evidence_in_source(span, source_text):
                    w.append(f"ci.{name}: evidence not found in source: {span[:80]!r}")
    for fld in ("social_context", "purpose"):
        for span in getattr(ex.evidence, fld):
            if not evidence_in_source(span, source_text):
                w.append(f"evidence.{fld}: not found in source: {span[:80]!r}")
        if getattr(ex.context, fld) and not getattr(ex.evidence, fld):
            w.append(f"context.{fld}: value without evidence")
    for fld in ("explicit_permissions", "explicit_restrictions"):
        kept = []
        for st in getattr(ex.context, fld):
            if evidence_in_source(st.evidence, source_text):
                kept.append(st)
            else:
                w.append(f"context.{fld}: DROPPED unsupported claim {st.text[:80]!r} (evidence not in source)")
        setattr(ex.context, fld, kept)
    kept_rel = []
    for r in ex.context.relationships:
        if evidence_in_source(r.evidence, source_text):
            kept_rel.append(r)
        else:
            w.append(f"context.relationships: DROPPED unsupported relationship {r.relation[:60]!r} (evidence not in source)")
    ex.context.relationships = kept_rel
    d = ex.domain
    if d.supporting_span and not evidence_in_source(d.supporting_span, source_text):
        w.append(f"domain: supporting_span not found in source: {d.supporting_span[:80]!r}")
    return ex, w


def merge_ci(ex: LLMExtraction, record: SourceRecord, blind_native_ci: bool = False) -> dict[str, FinalCIParameter]:
    """Benchmark-provided CI values always win and are never overwritten; LLM values fill the rest.

    With `blind_native_ci` (evaluation runs) the model was not shown the benchmark values, so every parameter is the
    model's own; the benchmark values remain untouched in the record's `native_ci`.
    """
    out: dict[str, FinalCIParameter] = {}
    for name in CI_PARAMS:
        native = None if blind_native_ci else record.native_ci.get(name)
        if native is not None:
            out[name] = FinalCIParameter(
                value=native.value, evidence=[], missing_reason=None, origin=native.origin, source_field=native.source_field, note=native.note
            )
            continue
        p = getattr(ex.ci_parameters, name)
        out[name] = FinalCIParameter(
            value=p.value, evidence=p.evidence, missing_reason=p.missing_reason, origin="llm" if p.value is not None else "none"
        )
    return out
