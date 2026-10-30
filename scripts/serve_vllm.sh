#!/usr/bin/env bash
# Serve a model with vLLM's OpenAI-compatible server. ci_audit connects to this server; it never loads models itself.
#
# Usage:  scripts/serve_vllm.sh [MODEL] [extra vllm args...]
#   MODEL                 default: meta-llama/Llama-3.1-8B-Instruct (gated on Hugging Face: accept the licence and set HF_TOKEN)
# Environment overrides:
#   HOST=127.0.0.1  PORT=8000  MAX_MODEL_LEN=4096  TP=1  GPU_MEM_UTIL=0.90  QUANT=""  DTYPE=auto  API_KEY=""  SERVED_NAME=""
#
# Adjusting for your hardware (the defaults will NOT fit every GPU):
#   * Out of memory at load      -> lower MAX_MODEL_LEN, lower GPU_MEM_UTIL (e.g. 0.80), use a smaller/quantised model,
#                                   or split the model across GPUs with TP=2 (TP must divide the attention-head count).
#   * Model too big for one GPU  -> TP=<number of GPUs>; or QUANT=fp8 / awq / gptq (needs a matching checkpoint, or fp8 on
#                                   recent NVIDIA GPUs for an unquantised checkpoint).
#   * KV-cache errors at runtime -> lower MAX_MODEL_LEN or the client --concurrency.
#   * Long prompts (PrivacyLens/MPCI trajectories, PiSAs scenarios) need MAX_MODEL_LEN of 16384+ and the memory for it;
#     ConfAIde Tier 2 fits easily in 4096.
#   * Pick GPUs with CUDA_VISIBLE_DEVICES=0,1 before calling this script.
# Then:  python -m ci_audit extract --engine vllm --model "<served name>" --base-url http://127.0.0.1:$PORT/v1 ...
# Check what is served:  curl -s http://127.0.0.1:8000/v1/models
set -euo pipefail

MODEL="${1:-meta-llama/Llama-3.1-8B-Instruct}"
[[ $# -gt 0 ]] && shift
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-4096}"
TP="${TP:-1}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.90}"
DTYPE="${DTYPE:-auto}"

args=(serve "$MODEL" --host "$HOST" --port "$PORT" --max-model-len "$MAX_MODEL_LEN"
      --tensor-parallel-size "$TP" --gpu-memory-utilization "$GPU_MEM_UTIL" --dtype "$DTYPE")
[[ -n "${QUANT:-}" ]] && args+=(--quantization "$QUANT")
[[ -n "${API_KEY:-}" ]] && args+=(--api-key "$API_KEY")        # then export VLLM_API_KEY for ci_audit
[[ -n "${SERVED_NAME:-}" ]] && args+=(--served-model-name "$SERVED_NAME")

echo "+ vllm ${args[*]} $*" >&2
exec vllm "${args[@]}" "$@"
