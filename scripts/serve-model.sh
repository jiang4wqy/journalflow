#!/usr/bin/env bash
set -euo pipefail

exec vllm serve "${MODEL_PATH:-PaddlePaddle/PaddleOCR-VL-1.6}" \
  --served-model-name PaddleOCR-VL-1.6-0.9B \
  --host 127.0.0.1 --port "${MODEL_PORT:-8118}" \
  --dtype bfloat16 --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION:-0.68}" \
  --max-model-len 16384 --max-num-seqs 32 --max-num-batched-tokens 16384 \
  --no-enable-prefix-caching --mm-processor-cache-gb 0
