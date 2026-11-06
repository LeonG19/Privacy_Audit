#!/usr/bin/env bash
# Serve RedHatAI/Llama-3.3-70B-Instruct-quantized.w4a16 (the open-source model of the pilot) with vLLM.
#
# Checkpoint: compressed-tensors INT4 weights / 16-bit activations (~40 GB of weights). vLLM detects the
# quantisation from the checkpoint, so QUANT is NOT set. The W4A16 (Marlin) kernels need an NVIDIA GPU with
# compute capability >= 8.0 (A100, A6000, L40S, H100 ...); V100s will not run it.
#
# Fits:   1x 80 GB (A100-80 / H100-80)            -> TP=1 (default)
#         2x 40-48 GB (A100-40 / A6000 / L40S)     -> TP=2
# Llama licence: accept it on the model page and export HF_TOKEN before the first download.
#
# Usage:  scripts/serve_llama70b.sh [extra vllm args...]
#   Env:  TP=1 MAX_MODEL_LEN=16384 GPU_MEM_UTIL=0.90 PORT=8000 HOST=127.0.0.1 (see serve_vllm.sh)
# Pilot items are at most ~11k characters (MPCI trajectories), i.e. well under 16k tokens with the prompt and
# a 3000-token answer, so 16384 is enough and leaves memory for KV cache.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export MAX_MODEL_LEN="${MAX_MODEL_LEN:-16384}"
export TP="${TP:-1}"
export GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.90}"
MODEL="${MODEL:-RedHatAI/Llama-3.3-70B-Instruct-quantized.w4a16}"

# --enable-prefix-caching: every request repeats the same rules/taxonomy/schema prefix.
# --generation-config vllm: use vLLM's neutral sampling defaults instead of the checkpoint's
#   generation_config.json (temperature 0.6, top_p 0.9), so only what ci_audit sends (temperature 0, seed) applies.
exec "$here/serve_vllm.sh" "$MODEL" --enable-prefix-caching --generation-config vllm "$@"
