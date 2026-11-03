"""GPU telemetry for self-hosted runs: sample `nvidia-smi` into a CSV with UTC timestamps, then summarise any time window.

Timestamps are taken on this side (epoch seconds, UTC) rather than from nvidia-smi's local-time stamp, so the log can be
sliced exactly by the `started_at` / `finished_at` (UTC) of each run manifest.
"""
from __future__ import annotations

import csv
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

QUERY = "index,name,utilization.gpu,memory.used,memory.total,power.draw"
FIELDS = ["utc_epoch", "sample", "gpu_index", "name", "util_pct", "mem_used_mib", "mem_total_mib", "power_w"]


def _gpu_selection() -> list[str]:
    """Only the GPUs this job may use (CUDA_VISIBLE_DEVICES), so idle GPUs on a shared host are not counted."""
    vis = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
    return ["-i", vis] if vis and vis not in ("NoDevFiles", "-1") else []


def monitor(out: Path, interval_s: float = 1.0) -> int:
    """Append one row per GPU per sample until SIGTERM/SIGINT. Returns 0 on a clean stop.

    One `nvidia-smi` call per sample, timestamped when it returns: streaming mode (`-lms`) would be timestamped when the
    pipe delivers the text, which buffering can delay. All rows of one call share `sample` and `utc_epoch`.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    new = not out.exists() or out.stat().st_size == 0
    cmd = ["nvidia-smi", f"--query-gpu={QUERY}", "--format=csv,noheader,nounits", *_gpu_selection()]
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    n = 0
    with open(out, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(FIELDS)
        while not stop["flag"]:
            t0 = time.monotonic()
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            except FileNotFoundError:
                print("nvidia-smi not found: GPU monitoring needs an NVIDIA driver on this host", file=sys.stderr)
                return 127
            except subprocess.TimeoutExpired:
                continue
            if res.returncode != 0:
                print(f"nvidia-smi failed ({res.returncode}): {res.stderr.strip()[:200]}", file=sys.stderr)
                return res.returncode
            ts = f"{time.time():.3f}"
            for line in res.stdout.splitlines():
                parts = [x.strip() for x in line.split(",")]
                if len(parts) == 6:
                    w.writerow([ts, n, *parts])
            f.flush()
            n += 1
            time.sleep(max(0.0, interval_s - (time.monotonic() - t0)))
    return 0


def _num(v: str) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):  # "[N/A]" on GPUs that do not report a field
        return None


def read_log(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [{**r, "utc_epoch": float(r["utc_epoch"])} for r in csv.DictReader(f)]


def summarize(rows: list[dict], start: float, end: float) -> Optional[dict]:
    """Utilisation / memory / power over [start, end] (epoch seconds). None when the log does not cover the window."""
    return summarize_windows(rows, [(start, end)])


def summarize_windows(rows: list[dict], windows: list[tuple[float, float]]) -> Optional[dict]:
    """Same as `summarize` over several disjoint windows (a run done in batches); energy uses their total duration."""
    win = [r for r in rows if any(s <= r["utc_epoch"] <= e for s, e in windows)]
    if not win:
        return None
    gpus = sorted({r["gpu_index"] for r in win})
    by_t: dict[str, list[dict]] = {}  # one entry per nvidia-smi call: sums across GPUs of the SAME sample only
    for r in win:
        by_t.setdefault(r.get("sample") or str(r["utc_epoch"]), []).append(r)
    util = [u for r in win if (u := _num(r["util_pct"])) is not None]
    mem_per_t = [sum(_num(r["mem_used_mib"]) or 0 for r in rs) for rs in by_t.values()]
    power_per_t = [sum(_num(r["power_w"]) or 0 for r in rs) for rs in by_t.values()]
    duration = sum(e - s for s, e in windows)
    mean_power = sum(power_per_t) / len(power_per_t) if power_per_t else None
    return {
        "n_gpus": len(gpus),
        "gpu_names": sorted({r["name"] for r in win}),
        "samples": len(by_t),
        "mean_util_pct": round(sum(util) / len(util), 1) if util else None,
        "peak_mem_used_gib_all_gpus": round(max(mem_per_t) / 1024, 2) if mem_per_t else None,
        "mem_total_gib_per_gpu": round((_num(win[0]["mem_total_mib"]) or 0) / 1024, 1),
        "mean_power_w_all_gpus": round(mean_power, 1) if mean_power is not None else None,
        "energy_wh": round(mean_power * duration / 3600, 2) if mean_power is not None else None,
    }
