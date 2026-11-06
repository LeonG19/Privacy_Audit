"""Pydantic schemas.

Layers that must never be silently merged (see README "Research safeguards"):

1. Original benchmark information  -> SourceRecord / ExtractionRecord.{original_text, native_*}
2. LLM-extracted information       -> LLMExtraction (and ExtractionRecord.ci_parameters[*].origin == "llm")
3. Researcher-verified information -> ExtractionRecord.researcher_review (empty until a human fills it)
4. Model-predicted privacy labels  -> PrivacyPredictionRecord (a separate file, joined by record_id)
5. Human-validated judgments       -> not implemented yet

LLM-facing models (CIParameter ... LLMExtraction) deliberately have no defaults and no free-form dicts so
that they convert to a *strict* JSON schema accepted by OpenAI structured outputs and Anthropic
`output_config.format`.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1"
CI_PARAMS = ("sender", "subject", "recipient", "information_type", "transmission_principle")


# --------------------------------------------------------------------------- source layer
class NativeReference(BaseModel):
    """The benchmark's own label, in its own format. Never converted to binary."""

    kind: str = Field(
        description="continuous_human_rating | pair_polarity | binary_label | attribute_appropriateness | "
        "violation_by_construction | none"
    )
    label: Any = None
    scale: Optional[dict[str, Any]] = None
    description: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)


class NativeCIValue(BaseModel):
    """A CI value taken directly from structured benchmark fields."""

    value: str
    source_field: str
    origin: Literal["benchmark", "benchmark_mapped"] = "benchmark"
    note: Optional[str] = Field(None, description="Present when the adapter had to map a benchmark role to a CI role.")


class SourceRecord(BaseModel):
    """What an adapter yields. Built from raw files only; nothing here comes from an LLM."""

    record_id: str
    dataset: str
    subset: Optional[str] = None
    level: Optional[str] = None
    source_id: str
    source_revision: str
    source_files: list[str]
    content_sha256: str = Field(description="sha256 of the exact original content this record was built from")
    original_text: str = Field(description="Exact original text (for AirGapAgent-R: rendered from fields, see text_is_rendered)")
    scenario_text: str = Field(description="Deterministic view of original_text shown to the model (instructions/prompt boilerplate removed)")
    text_is_rendered: bool = False
    native_reference: NativeReference
    native_ci: dict[str, NativeCIValue] = Field(default_factory=dict)
    native_domain: Optional[str] = None
    source_metadata: dict[str, Any] = Field(default_factory=dict, description="Benchmark-provided provenance/annotations kept verbatim in outputs (e.g. PrivacyLens source_details, PiSAs visibility rules)")
    native_fields: dict[str, Any] = Field(default_factory=dict, description="Other original structured fields (not copied into outputs; re-derivable from source_id)")
    prompt_context: dict[str, Any] = Field(default_factory=dict, description="Extra template variables (no labels!)")
    links: dict[str, Any] = Field(default_factory=dict)
    assets: dict[str, Any] = Field(default_factory=dict)
    parse_warnings: list[str] = Field(default_factory=list)
    privacy_classification_supported: bool = False


# --------------------------------------------------------------------------- LLM-facing layer
class CIParameter(BaseModel):
    value: Optional[str] = Field(description="The parameter as stated or directly implied by the text; null if not stated.")
    evidence: list[str] = Field(description="Verbatim spans copied from the source text supporting `value`. Empty only if value is null.")
    missing_reason: Optional[str] = Field(description="Required when value is null: why it cannot be extracted.")


class CIParameters(BaseModel):
    sender: CIParameter
    subject: CIParameter
    recipient: CIParameter
    information_type: CIParameter
    transmission_principle: CIParameter


class Relationship(BaseModel):
    actors: list[str]
    relation: str
    evidence: str


class Statement(BaseModel):
    text: str
    evidence: str


class Context(BaseModel):
    social_context: Optional[str] = Field(description="The setting/institution in which the flow occurs, if stated.")
    purpose: Optional[str] = Field(description="Purpose or task of the flow. Distinct from transmission_principle.")
    relationships: list[Relationship]
    explicit_permissions: list[Statement] = Field(description="Explicit consent/permission/authorisation stated in the text.")
    explicit_restrictions: list[Statement] = Field(description="Explicit restrictions/confidentiality/prohibitions stated in the text.")
    unknown_facts: list[str] = Field(description="Facts a judgement would need that the text does not state.")


class ContextEvidence(BaseModel):
    social_context: list[str]
    purpose: list[str]


class DomainPrediction(BaseModel):
    domain: str = Field(description="Prefer a label from the provided taxonomy; 'Unclear' if ambiguous; 'Other' with a proposed name in subdomain.")
    subdomain: Optional[str]
    explanation: str = Field(description="One or two sentences justified by the information flow's context, not by keywords.")
    supporting_span: Optional[str] = Field(description="Verbatim span from the source text, or null.")
    confidence: Literal["high", "medium", "low"]
    needs_review: bool


class LLMExtraction(BaseModel):
    ci_parameters: CIParameters
    context: Context
    evidence: ContextEvidence
    domain: DomainPrediction


class PrivacyPrediction(BaseModel):
    label: Literal["appropriate", "inappropriate", "uncertain"]
    rationale: str
    key_factors: list[str]
    confidence: Literal["high", "medium", "low"]


# --------------------------------------------------------------------------- output layer
class FinalCIParameter(CIParameter):
    origin: Literal["benchmark", "benchmark_mapped", "llm", "none"]
    source_field: Optional[str] = None
    note: Optional[str] = None


class FinalDomain(DomainPrediction):
    in_taxonomy: bool
    taxonomy_version: Optional[int] = None
    review_reasons: list[str] = Field(default_factory=list)


class ResearcherReview(BaseModel):
    """Placeholder for researcher-verified information. Extraction never writes to it."""

    status: Literal["unreviewed", "verified", "corrected", "rejected"] = "unreviewed"
    reviewer: Optional[str] = None
    reviewed_at: Optional[str] = None
    corrections: dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None


class ExtractionMetadata(BaseModel):
    run_id: str
    model: str
    engine: str
    is_mock: bool = False
    base_url: Optional[str] = None
    prompt_version: str
    prompt_sha256: str
    generation: dict[str, Any]
    structured_output_mode: Optional[str] = None
    status: Literal["ok", "ok_with_warnings"]
    attempts: int = 1
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_s: Optional[float] = None
    finish_reason: Optional[str] = None
    source_truncated: bool = False
    processed_at: str
    schema_version: str = SCHEMA_VERSION
    raw_response: Optional[str] = None


class ExtractionRecord(BaseModel):
    schema_version: str = SCHEMA_VERSION
    # --- layer 1: original benchmark information
    record_id: str
    dataset: str
    subset: Optional[str] = None
    level: Optional[str] = None
    source_id: str
    source_revision: str
    source_files: list[str]
    content_sha256: str
    original_text: str
    scenario_text: str
    text_is_rendered: bool = False
    native_reference: NativeReference
    native_ci: dict[str, NativeCIValue] = Field(default_factory=dict)
    native_domain: Optional[str] = None
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    links: dict[str, Any] = Field(default_factory=dict)
    assets: dict[str, Any] = Field(default_factory=dict)
    # --- layer 2: LLM extraction (each CI parameter carries its origin)
    ci_parameters: dict[str, FinalCIParameter]
    context: Context
    evidence: ContextEvidence
    domain: FinalDomain
    validation_warnings: list[str] = Field(default_factory=list)
    # --- layer 3: researcher verification (empty)
    researcher_review: ResearcherReview = Field(default_factory=ResearcherReview)
    extraction_metadata: ExtractionMetadata


class PrivacyPredictionRecord(BaseModel):
    """Layer 4. Stored separately from extractions; NEVER a benchmark label."""

    record_id: str
    dataset: str
    source_id: str
    kind: Literal["model_prediction"] = "model_prediction"
    note: str = "Model-predicted label. Independent of, and never merged into, the benchmark's native reference."
    prediction: PrivacyPrediction
    extraction_metadata: ExtractionMetadata


class FailureRecord(BaseModel):
    record_id: str
    dataset: str
    source_id: str
    stage: Literal["api", "parse", "validation", "other"]
    error_type: str
    message: str
    attempts: int
    raw_response: Optional[str] = None
    validation_errors: list[str] = Field(default_factory=list)
    processed_at: str
    run_id: str
    usage: dict[str, Any] = Field(default_factory=dict, description="Tokens spent on this record before it failed (all calls)")
    latency_s: Optional[float] = None


# --------------------------------------------------------------------------- strict JSON schema helper
_UNSUPPORTED_KEYS = {"default", "title", "minLength", "maxLength", "minItems", "maxItems", "pattern", "format"}


def strict_json_schema(model: type[BaseModel]) -> dict:
    """JSON schema for `model` in the 'strict' dialect: additionalProperties=false, every property required.

    Descriptions are kept (they steer the model); keywords strict decoders reject are removed.
    """
    schema = model.model_json_schema()

    def fix(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in _UNSUPPORTED_KEYS:
                    continue
                if k in ("properties", "$defs"):  # mappings of names -> schemas: never filter the names
                    out[k] = {name: fix(sub) for name, sub in v.items()}
                else:
                    out[k] = fix(v)
            node = out
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            return node
        if isinstance(node, list):
            return [fix(v) for v in node]
        return node

    return fix(schema)
