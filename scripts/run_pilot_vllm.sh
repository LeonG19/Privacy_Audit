#!/usr/bin/env bash
# Llama-3.3-70B pilot, end to end on ONE GPU host: start vLLM, log GPU telemetry, extract the shared pilot sample
# for all five datasets (blind to benchmark CI values), stop everything. Use directly on a GPU machine or via
# scripts/slurm/pilot_llama70b.sbatch.
#
# Env: CONCURRENCY=8 PORT=8000 PILOT_DIR=pilot RESUME=0 (+ serve_llama70b.sh variables: TP, MAX_MODEL_LEN, ...)
#      RESUME=1 after a timeout/crash: datasets already started continue (--resume) instead of failing.
# Output: $PILOT_DIR/runs/<dataset>/vllm__<model>/   $PILOT_DIR/runs/gpu_metrics.csv   $PILOT_DIR/runs/vllm_server.log
# Then (anywhere):  python -m ci_audit pilot score --gpu-log $PILOT_DIR/runs/gpu_metrics.csv
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

MODEL="${MODEL:-RedHatAI/Llama-3.3-70B-Instruct-quantized.w4a16}"
PORT="${PORT:-8000}"
CONCURRENCY="${CONCURRENCY:-8}"
PILOT_DIR="${PILOT_DIR:-pilot}"
DATASETS="${DATASETS:-confaide_tier2 privacylens mpci_bench airgapagent_r pisas}"
RUNS="$PILOT_DIR/runs"
mkdir -p "$RUNS"
PY="${PYTHON:-python}"

RESUME_FLAG=()
[[ "${RESUME:-0}" == 1 ]] && RESUME_FLAG=(--resume)
ready() { curl -sf --noproxy '*' "http://127.0.0.1:$PORT/v1/models" >/dev/null; }   # cluster proxies must not see localhost

pids=()
cleanup() {  # TERM, then KILL whatever is still alive after 60 s (never hang until the Slurm limit)
  for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done
  for _ in $(seq 1 60); do
    alive=0; for p in "${pids[@]}"; do kill -0 "$p" 2>/dev/null && alive=1; done
    [[ $alive == 0 ]] && return; sleep 1
  done
  for p in "${pids[@]}"; do kill -9 "$p" 2>/dev/null || true; done
}
trap cleanup EXIT

echo "=== $(date -u +%FT%TZ) server start" >>"$RUNS/vllm_server.log"
PORT="$PORT" MODEL="$MODEL" scripts/serve_llama70b.sh >>"$RUNS/vllm_server.log" 2>&1 &
pids+=($!)
echo "waiting for vLLM on :$PORT (first start downloads ~40 GB) ..."
for _ in $(seq 1 360); do   # up to 60 min
  ready && break
  kill -0 "${pids[0]}" 2>/dev/null || { echo "vLLM exited; see $RUNS/vllm_server.log" >&2; exit 1; }
  sleep 10
done
ready || { echo "vLLM not ready after 60 min" >&2; exit 1; }

"$PY" -m ci_audit gpu-monitor --out "$RUNS/gpu_metrics.csv" --interval 1 &
pids+=($!)
sleep 5
[[ $(wc -l <"$RUNS/gpu_metrics.csv" 2>/dev/null || echo 0) -gt 1 ]] || { echo "GPU monitor wrote no samples; aborting" >&2; exit 1; }
nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv >"$RUNS/gpu_inventory.csv" || true
"$PY" -c "import vllm; print(vllm.__version__)" >"$RUNS/vllm_version.txt" 2>/dev/null || true

status=0
for ds in $DATASETS; do
  echo "=== $ds"
  "$PY" -m ci_audit extract --dataset "$ds" --engine vllm --model "$MODEL" --base-url "http://127.0.0.1:$PORT/v1" \
    --ids-file "$PILOT_DIR/pilot_sample.json" --blind-native-ci --concurrency "$CONCURRENCY" \
    --output-dir "$RUNS" --no-progress ${RESUME_FLAG[@]+"${RESUME_FLAG[@]}"} || status=1   # failures are recorded per record; keep going
  # keep this session's manifest: a later --resume overwrites run_manifest.json, and scoring needs every session's time window
  for m in "$RUNS/$ds"/vllm__*/run_manifest.json; do [[ -f "$m" ]] && cp "$m" "${m%.json}.$(date -u +%Y%m%dT%H%M%S).json"; done
done
exit $status
