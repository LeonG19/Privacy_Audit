#!/usr/bin/env bash
# Slurm job for the Llama-3.3-70B pilot. Adjust partition / GPU type / account to your cluster before submitting.
# Needs ONE 80 GB Ampere-or-newer GPU (or two 40-48 GB with TP=2). V100s cannot run the W4A16 kernels.
#
#   sbatch scripts/slurm/pilot_llama70b.sbatch
#   (two smaller GPUs: change --gpus to 2 of that type and add --export=ALL,TP=2)
#
#SBATCH --job-name=ci-pilot-llama70b
#SBATCH --partition=GPU-shared
#SBATCH --gpus=h100-80:1
#SBATCH --time=03:00:00
#SBATCH --output=pilot/runs/slurm-%j.out
set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-.}"
# Activate the environment that has vllm + this package (pip install -e . && pip install vllm), e.g.:
# module load anaconda3 && conda activate ci-audit
: "${HF_TOKEN:?export HF_TOKEN (Llama licence accepted on Hugging Face) before submitting}"
export HF_HOME="${HF_HOME:-$PWD/.hf_cache}"   # ~40 GB; point at project/scratch storage, not $HOME
scripts/run_pilot_vllm.sh
