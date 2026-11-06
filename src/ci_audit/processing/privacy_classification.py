"""Optional, SEPARATE operation: does the model judge the described flow appropriate?

The prompt contains only the scenario text. It never contains the benchmark's native reference, the extracted CI
values, or record identifiers. The prediction is written to its own file and is never merged into extraction
records or compared silently with the native label.
"""
from __future__ import annotations

from ci_audit.engines.base import Engine, GenerationSettings
from ci_audit.processing.ci_extraction import ExtractionFailure, _Fail, call_and_parse, make_metadata
from ci_audit.prompts import PromptBuilder
from ci_audit.schemas import FailureRecord, PrivacyPrediction, PrivacyPredictionRecord, SourceRecord
from ci_audit.storage import utcnow


async def classify_record(engine: Engine, record: SourceRecord, builder: PromptBuilder, settings: GenerationSettings,
                          run_id: str, validation_retries: int = 2) -> PrivacyPredictionRecord:
    if not record.privacy_classification_supported:
        raise NotImplementedError(f"privacy classification is not implemented for dataset {record.dataset!r}")
    prompt = builder.render_privacy(record, PrivacyPrediction)
    try:
        pred, resp, attempts, raw = await call_and_parse(engine, prompt.system, prompt.user, PrivacyPrediction, settings, validation_retries)
    except _Fail as f:
        raise ExtractionFailure(
            FailureRecord(record_id=record.record_id, dataset=record.dataset, source_id=record.source_id, stage=f.stage, error_type=f.etype,
                          message=f.msg[:1500], attempts=f.attempts, raw_response=f.raw, validation_errors=f.errors, processed_at=utcnow(), run_id=run_id,
                          usage=f.spent.usage(), latency_s=f.spent.latency_s())
        ) from None
    meta = make_metadata(engine, resp, prompt, settings, attempts, run_id, raw, False)
    return PrivacyPredictionRecord(record_id=record.record_id, dataset=record.dataset, source_id=record.source_id, prediction=pred, extraction_metadata=meta)
