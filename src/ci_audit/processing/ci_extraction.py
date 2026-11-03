"""One-record CI extraction: render prompt -> call engine -> parse -> validate -> merge with benchmark values."""
from __future__ import annotations

from typing import Optional

from ci_audit.engines.base import Engine, EngineError, GenerationSettings
from ci_audit.processing.domain_classification import finalize_domain
from ci_audit.processing.validation import ResponseParseError, check_extraction, merge_ci, parse_json_response, validate_model
from ci_audit.prompts import PromptBuilder, Taxonomy
from ci_audit.schemas import ExtractionMetadata, ExtractionRecord, FailureRecord, LLMExtraction, SourceRecord
from ci_audit.storage import utcnow


class ExtractionFailure(Exception):
    def __init__(self, failure: FailureRecord):
        super().__init__(failure.message)
        self.failure = failure


def _feedback(errors: list[str]) -> str:
    return (
        "\n\n[Your previous reply was rejected: " + "; ".join(errors[:8]) + ". "
        "Reply again with ONE JSON object that conforms exactly to the schema, and nothing else.]"
    )


async def call_and_parse(engine: Engine, system: str, user: str, model_cls, settings: GenerationSettings, validation_retries: int):
    """Shared by extraction and privacy classification. Returns (instance, response, attempts, raw_text).

    Raises ExtractionFailure-compatible tuples via _Fail so callers can build a FailureRecord.
    """
    errors: list[str] = []
    resp = None
    raw = None
    attempts = 0
    spent = _Spent()
    for attempt in range(validation_retries + 1):
        attempts += 1
        prompt = user + (_feedback(errors) if errors else "")
        try:
            resp = await engine.generate(system, prompt, model_cls, settings)
        except EngineError as e:
            raise _Fail("api", type(e).__name__, str(e), attempts, raw, errors, spent) from e
        spent.add(resp)
        raw = resp.content
        if resp.finish_reason == "length":
            errors = ["output was cut off at the token limit"]
            if attempt == validation_retries:
                raise _Fail("parse", "TruncatedOutput", "model output hit max_output_tokens; raise --max-output-tokens", attempts, raw, errors, spent)
            continue
        if resp.finish_reason == "refusal":
            raise _Fail("other", "ModelRefusal", "the model refused to answer", attempts, raw, errors, spent)
        try:
            obj = parse_json_response(raw)
        except ResponseParseError as e:
            errors = [str(e)]
            continue
        inst, errs = validate_model(obj, model_cls)
        if inst is not None:
            resp.usage, resp.latency_s = spent.usage(), spent.latency_s()
            return inst, resp, attempts, raw
        errors = errs
    stage = "parse" if errors and not any(":" in e for e in errors) else "validation"
    raise _Fail(stage, "InvalidModelOutput", "; ".join(errors[:5]), attempts, raw, errors, spent)


class _Spent:
    """Tokens and time over every model call for one record (re-asks included), so cost is not under-counted."""

    def __init__(self):
        self.calls: list[dict] = []

    def add(self, resp) -> None:
        self.calls.append({**resp.usage, "latency_s": resp.latency_s})

    def _sum(self, key: str):
        vals = [c.get(key) for c in self.calls]
        return None if not vals or any(v is None for v in vals) else sum(vals)

    def usage(self) -> dict:
        out = {"prompt_tokens": self._sum("prompt_tokens"), "completion_tokens": self._sum("completion_tokens"), "calls": len(self.calls)}
        if len(self.calls) > 1:
            out["per_call"] = self.calls
        return out

    def latency_s(self):
        total = self._sum("latency_s")
        return None if total is None else round(total, 3)


class _Fail(Exception):
    def __init__(self, stage, etype, msg, attempts, raw, errors, spent=None):
        super().__init__(msg)
        self.stage, self.etype, self.msg, self.attempts, self.raw, self.errors = stage, etype, msg, attempts, raw, errors
        self.spent = spent or _Spent()


def make_metadata(engine: Engine, resp, prompt, settings: GenerationSettings, attempts: int, run_id: str, raw: str, warnings: bool) -> ExtractionMetadata:
    d = engine.describe()
    return ExtractionMetadata(
        run_id=run_id,
        model=resp.model or engine.model,
        engine=engine.name,
        is_mock=engine.is_mock,
        base_url=d.get("base_url"),
        prompt_version=prompt.version,
        prompt_sha256=prompt.sha256,
        generation={**settings.as_dict(), "dropped_params": resp.dropped_params, "requested_model": engine.model},
        structured_output_mode=resp.structured_mode,
        status="ok_with_warnings" if warnings else "ok",
        attempts=attempts,
        usage=resp.usage,
        latency_s=resp.latency_s,
        finish_reason=resp.finish_reason,
        source_truncated=prompt.source_truncated,
        processed_at=utcnow(),
        raw_response=raw,
    )


async def extract_record(engine: Engine, record: SourceRecord, builder: PromptBuilder, settings: GenerationSettings,
                         taxonomy: Taxonomy, run_id: str, validation_retries: int = 2, blind_native_ci: bool = False) -> ExtractionRecord:
    prompt = builder.render_extraction(record, LLMExtraction, blind_native_ci)
    try:
        ex, resp, attempts, raw = await call_and_parse(engine, prompt.system, prompt.user, LLMExtraction, settings, validation_retries)
    except _Fail as f:
        raise ExtractionFailure(
            FailureRecord(record_id=record.record_id, dataset=record.dataset, source_id=record.source_id, stage=f.stage, error_type=f.etype,
                          message=f.msg[:1500], attempts=f.attempts, raw_response=f.raw, validation_errors=f.errors, processed_at=utcnow(), run_id=run_id,
                          usage=f.spent.usage(), latency_s=f.spent.latency_s())
        ) from None
    ex, warnings = check_extraction(ex, record.scenario_text)
    if prompt.source_truncated:
        warnings.append("source text was truncated in the prompt")
    meta = make_metadata(engine, resp, prompt, settings, attempts, run_id, raw, bool(warnings))
    return ExtractionRecord(
        record_id=record.record_id, dataset=record.dataset, subset=record.subset, level=record.level, source_id=record.source_id,
        source_revision=record.source_revision, source_files=record.source_files, content_sha256=record.content_sha256,
        original_text=record.original_text, scenario_text=record.scenario_text, text_is_rendered=record.text_is_rendered,
        native_reference=record.native_reference, native_ci=record.native_ci, native_domain=record.native_domain,
        source_metadata=record.source_metadata, links=record.links, assets=record.assets,
        ci_parameters=merge_ci(ex, record, blind_native_ci), context=ex.context, evidence=ex.evidence,
        domain=finalize_domain(ex.domain, taxonomy), validation_warnings=warnings, extraction_metadata=meta,
    )
