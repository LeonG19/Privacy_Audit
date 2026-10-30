"""Adapter registry."""
from __future__ import annotations

import difflib

from ci_audit.datasets.airgapagent_r import AirGapAgentRAdapter
from ci_audit.datasets.base import AdapterError, DatasetAdapter
from ci_audit.datasets.confaide import ConfaideTier2Adapter, ConfaideTier3Adapter
from ci_audit.datasets.mpci_bench import MPCIBenchAdapter
from ci_audit.datasets.pisas import PiSAsAdapter
from ci_audit.datasets.privacylens import PrivacyLensAdapter

ADAPTERS: dict[str, type[DatasetAdapter]] = {
    a.name: a
    for a in (ConfaideTier2Adapter, ConfaideTier3Adapter, PrivacyLensAdapter, MPCIBenchAdapter, AirGapAgentRAdapter, PiSAsAdapter)
}


def get_adapter_class(name: str) -> type[DatasetAdapter]:
    if name not in ADAPTERS:
        hint = difflib.get_close_matches(name, ADAPTERS, n=1)
        msg = f"Unknown dataset {name!r}. Available: {', '.join(sorted(ADAPTERS))}."
        if hint:
            msg += f" Did you mean {hint[0]!r}?"
        raise AdapterError(msg)
    return ADAPTERS[name]
