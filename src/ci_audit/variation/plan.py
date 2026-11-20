"""Choose the replacement values. Pure and deterministic: the same (seed, record, parameter, k) always draws the same value."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

from ci_audit.variation.knowledge import (CI_PARAMS, PARAM_KIND, ROLE_PARAMS, DomainKnowledge, PoolItem, norm_value)
from ci_audit.variation.seeds import Seed


@dataclass
class VariantPlan:
    variant_id: str
    seed: Seed
    parameter: str
    k: int
    original_value: str
    new_value: str
    new_ci: dict[str, Optional[str]]
    pool_kind: str
    pool_domain: str
    pool_scope: str                       # same_domain | cross_domain_fallback
    pool_size: int
    pool_sources: list[str] = field(default_factory=list)


def _usable(item: PoolItem, max_chars: int) -> bool:
    return len(item.value) <= max_chars and "@" not in item.value


def candidates(seed: Seed, param: str, kb: DomainKnowledge, max_chars: int = 200,
               strict_domain: bool = False) -> tuple[list[PoolItem], str]:
    """Pool values that may replace `seed.ci[param]`: same domain & kind, excluding
      * the original value itself (and, for roles, the other two roles already in the flow),
      * values that were extracted from this very record.
    Falls back to every domain (flagged) when the domain has nothing else to offer, unless strict_domain."""
    kind = PARAM_KIND[param]
    banned = {norm_value(seed.ci[param] or "")}
    if param in ROLE_PARAMS:
        banned |= {norm_value(seed.ci[p] or "") for p in ROLE_PARAMS if p != param}
    banned.discard("")

    def ok(i: PoolItem) -> bool:
        return (_usable(i, max_chars) and norm_value(i.value) not in banned
                and not (i.sources and set(i.sources) <= {seed.record_id}))

    same = [i for i in kb.items(seed.domain, kind) if ok(i)]
    if same or strict_domain:
        return same, "same_domain"
    return [i for i in kb.all_items(kind) if ok(i)], "cross_domain_fallback"


def plan_variants(seeds: list[Seed], kb: DomainKnowledge, rng_seed: int, params: tuple[str, ...] = CI_PARAMS,
                  per_param: int = 1, max_chars: int = 200, strict_domain: bool = False
                  ) -> tuple[list[VariantPlan], list[dict]]:
    """-> (plans, skipped). One plan per (seed, existing CI parameter, k). Parameters that are null in the sample are
    not touched (nothing to replace); parameters whose pool is empty are reported in `skipped`."""
    plans: list[VariantPlan] = []
    skipped: list[dict] = []
    for seed in seeds:
        for param in params:
            original = seed.ci.get(param)
            if not original:
                continue
            cands, scope = candidates(seed, param, kb, max_chars, strict_domain)
            if not cands:
                skipped.append({"record_id": seed.record_id, "parameter": param, "domain": seed.domain,
                                "reason": "no replacement value available in the pool"})
                continue
            rng = random.Random(f"{rng_seed}|{seed.record_id}|{param}")
            order = rng.sample(cands, len(cands))          # k-th variant takes the k-th distinct draw
            for k in range(min(per_param, len(order))):
                item = order[k]
                new_ci = dict(seed.ci)
                new_ci[param] = item.value
                plans.append(VariantPlan(
                    variant_id=f"{seed.record_id}::{param}::{k}", seed=seed, parameter=param, k=k,
                    original_value=original, new_value=item.value, new_ci=new_ci, pool_kind=item.kind,
                    pool_domain=item.domain, pool_scope=scope, pool_size=len(cands), pool_sources=item.sources[:5]))
    return plans, skipped
