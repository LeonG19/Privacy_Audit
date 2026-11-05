"""Prompt rendering. One extraction template per dataset; shared rules/blocks are Jinja includes."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import jinja2
import yaml
from pydantic import BaseModel

from ci_audit.config import Paths, load_config
from ci_audit.schemas import SourceRecord, strict_json_schema
from ci_audit.storage import sha256_text

BLIND_SUFFIX = "+blind_native_ci"  # benchmark CI values hidden from the model (see render_extraction)
TRUNCATION_MARK = "\n[... source text truncated by the pipeline (max_source_chars) ...]"


@dataclass
class Taxonomy:
    version: int
    domains: list[dict]  # [{"name","description"}]
    reserved: dict

    @property
    def names(self) -> list[str]:
        return [d["name"] for d in self.domains]


def load_taxonomy(root) -> Taxonomy:
    cfg = load_config(root, "domains")
    return Taxonomy(
        version=cfg["version"],
        domains=[{"name": k, "description": v} for k, v in cfg["domains"].items()],
        reserved=cfg["reserved"],
    )


@dataclass
class RenderedPrompt:
    system: str
    user: str
    version: str
    sha256: str
    source_truncated: bool


class PromptBuilder:
    def __init__(self, paths: Paths, extraction_cfg: dict, taxonomy: Taxonomy):
        self.paths = paths
        self.cfg = extraction_cfg
        self.taxonomy = taxonomy
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(paths.prompts)),
            undefined=jinja2.StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
        )

    # -- identification --------------------------------------------------------------
    def _entry(self, key: str) -> dict:
        try:
            return self.cfg["prompts"][key]
        except KeyError:
            raise KeyError(f"no prompt configured for {key!r} in configs/extraction.yaml") from None

    def template_name(self, key: str) -> str:
        e = self._entry(key)
        return f"{e['dir']}/{e.get('template', 'extract_ci.j2')}"

    def prompt_version(self, key: str, blind_native_ci: bool = False) -> str:
        return self._entry(key)["version"] + (BLIND_SUFFIX if blind_native_ci else "")

    def prompt_sha256(self, key: str, blind_native_ci: bool = False) -> str:
        """Hash of every file that can influence the prompt text (templates, shared rules, taxonomy)."""
        files = ["shared/system.md", "shared/extraction_rules.md", "shared/domain_block.j2", "shared/known_ci.j2", "shared/output_format.j2", self.template_name(key)]
        blob = "".join(self.env.loader.get_source(self.env, f)[0] for f in files)
        blob += yaml.safe_dump({"taxonomy": self.taxonomy.domains, "version": self.taxonomy.version}, sort_keys=True)
        if blind_native_ci:
            blob += BLIND_SUFFIX
        return sha256_text(blob)

    # -- rendering --------------------------------------------------------------------
    def _text(self, record: SourceRecord) -> tuple[str, bool]:
        limit = int(self.cfg.get("max_source_chars", 24000))
        t = record.scenario_text
        if len(t) > limit:
            return t[:limit] + TRUNCATION_MARK, True
        return t, False

    def _system(self) -> str:
        return self.env.get_template("shared/system.md").render().strip()

    @staticmethod
    def extraction_key(dataset: str, variant: Optional[str] = None) -> str:
        """Prompt config key: the dataset, or `<dataset>_<variant>` for an alternative template (e.g. `focused`)."""
        return f"{dataset}_{variant}" if variant else dataset

    def render_extraction(self, record: SourceRecord, schema: type[BaseModel], blind_native_ci: bool = False,
                          variant: Optional[str] = None) -> RenderedPrompt:
        """`blind_native_ci`: do not show benchmark-provided CI values, so the model extracts all five (evaluation runs)."""
        key = self.extraction_key(record.dataset, variant)
        text, truncated = self._text(record)
        ctx = {
            "variant": record.subset,
            "level": record.level,
            "scenario_text": text,
            "known_ci": {} if blind_native_ci else {k: v for k, v in record.native_ci.items()},
            "meta": {k: (v if v is not None else "not given") for k, v in record.prompt_context.items()},
            "taxonomy": self.taxonomy,
            "output_schema": json.dumps(strict_json_schema(schema), indent=1),
        }
        user = self.env.get_template(self.template_name(key)).render(**ctx)
        return RenderedPrompt(self._system(), user, self.prompt_version(key, blind_native_ci), self.prompt_sha256(key, blind_native_ci), truncated)

    def render_privacy(self, record: SourceRecord, schema: type[BaseModel]) -> RenderedPrompt:
        """Privacy-classification prompt: scenario text only. No native reference, no extracted values, no ids."""
        key = "privacy_classification"
        text, truncated = self._text(record)
        user = self.env.get_template(self.template_name(key)).render(
            scenario_text=text, output_schema=json.dumps(strict_json_schema(schema), indent=1)
        )
        sha = sha256_text(self.env.loader.get_source(self.env, self.template_name(key))[0] + self.env.loader.get_source(self.env, "shared/output_format.j2")[0])
        return RenderedPrompt(self._system(), user, self.prompt_version(key), sha, truncated)
