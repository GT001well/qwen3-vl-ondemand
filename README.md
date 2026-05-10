# Qwen3-VL On-Demand Relay

> English | [中文文档](README_zh.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue)

A lightweight on-demand model loading proxy for Qwen3-VL and any multimodal GGUF models.

> The model loads into VRAM only when a request arrives, and automatically unloads after idling — freeing GPU memory for other tasks.

---

## Why?

Running local LLMs — especially vision models — has an awkward problem:

- Keep it resident in VRAM and there's no room for other GPU workloads (image gen, training, games)
- Manually start/stop every time is a chore
- Model loading takes seconds — you don't want to wait on every request

This relay's approach: **keep a tiny proxy process that holds zero VRAM, and load the real model on demand.**

```
Idle:     relay listens on port 8083 ← 0 VRAM
Request:  relay → auto-spawns llama-server → model loads (~3.8GB)
Idle 5m:  relay → auto-kills llama-server → VRAM freed
```

---

## Features

- **Zero VRAM at rest** — the relay itself uses a few MB of RAM
- **Drop-in OpenAI-compatible API** — transparently proxies `/v1/chat/completions`, `/v1/models`, and everything else
- **Auto lifecycle** — request triggers loading, idle timeout triggers unloading
- **PDEATHSIG protection** — if the relay dies (even SIGKILL), llama-server auto-exits — no orphan processes
- **Model-agnostic** — swap models via environment variables, not limited to Qwen3-VL
- **Pure Python stdlib** — zero third-party dependencies

---

## Quick Start

### Prerequisites

- Python 3.8+
- [llama.cpp](https://github.com/ggml-org/llama.cpp) (CUDA build)
- A multimodal GGUF model + mmproj file
- NVIDIA GPU (for other backends, adjust `--n-gpu-layers` etc.)

### Setup

```bash
git clone https://github.com/GT001well/qwen3-vl-ondemand.git
cd qwen3-vl-ondemand
chmod +x start.sh stop.sh
```

### Configuration

Everything is configurable via environment variables with sensible defaults:

```bash
export VL_MODEL="/path/to/your/model.gguf"
export VL_MMPROJ="/path/to/your/mmproj.gguf"
export LLAMA_SERVER="/path/to/llama-server"
export VL_PORT=8083
export VL_INTERNAL_PORT=8084
export VL_IDLE_TIMEOUT=300   # seconds, default 5 minutes
export VL_CTX_SIZE=8192
```

You can also edit the constants at the top of `vl-relay.py`.

### Start

```bash
./start.sh
```

### Test

```bash
curl http://127.0.0.1:8083/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 100
  }'
```

The first request takes a couple seconds (model loading). Subsequent requests respond immediately.

### Stop

```bash
./stop.sh
```

Or just close the terminal — `start.sh` uses `exec`, so closing the terminal kills the relay, and PDEATHSIG kills llama-server.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `VL_MODEL` | `~/AI-Server/model/qwen3-vl/...gguf` | Path to GGUF model |
| `VL_MMPROJ` | `~/AI-Server/model/qwen3-vl/mmproj-F16.gguf` | Vision projection model |
| `LLAMA_SERVER` | `~/AI-Server/llama.cpp/build/bin/llama-server` | Path to llama-server binary |
| `VL_PORT` | `8083` | Public listening port |
| `VL_INTERNAL_PORT` | `8084` | Internal backend port |
| `VL_IDLE_TIMEOUT` | `300` | Idle timeout in seconds |
| `VL_POLL_INTERVAL` | `15` | Idle check interval (seconds) |
| `VL_CTX_SIZE` | `8192` | Context window size |
| `VL_N_GPU_LAYERS` | `99` | GPU offloading layers |
| `VL_PARALLEL` | `4` | Max parallel requests |

---

## How It Works

```
┌─────────────────────────────────────────────┐
│            Your Application                  │
│  (astrbot, Open WebUI, any OpenAI client)    │
└──────────────────┬──────────────────────────┘
                   │ POST /v1/chat/completions
                   ▼
┌─────────────────────────────────────────────┐
│            vl-relay.py (Python proxy)         │
│                                              │
│  1. Receive request → check if backend alive │
│  2. Not running → spawn llama-server + wait  │
│  3. Transparently proxy to internal port      │
│  4. Return response                           │
│  5. Reset idle timer                          │
│  6. Idle timeout → kill llama-server          │
└──────────────────┬──────────────────────────┘
                   │ local proxy
                   ▼
┌─────────────────────────────────────────────┐
│            llama-server (llama.cpp backend)   │
│  Port INT_PORT, ~3.8GB VRAM when active      │
│  Present on request, gone on timeout          │
└─────────────────────────────────────────────┘
```

**Key design decisions:**

**PDEATHSIG** — Uses Linux `prctl(PR_SET_PDEATHSIG, SIGTERM)` so the child exits when the relay dies. Even `kill -9` on the relay won't leave an orphan hogging VRAM.

**Exec on start** — `start.sh` uses `exec python3 vl-relay.py`, replacing the shell process. Close the terminal → relay dies → llama-server follows. No systemd service or nohup needed.

**Transparent proxy** — All HTTP methods (GET/POST/PUT/DELETE) pass through verbatim. It doesn't care about the API schema. Text chat, vision requests with image_url, model listing — all work automatically.

---

## Examples

### Text chat

```bash
curl http://127.0.0.1:8083/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-vl",
    "messages": [{"role": "user", "content": "Who are you?"}],
    "max_tokens": 100
  }'
```

### Vision (base64 image)

```bash
export IMAGE_B64=$(base64 -w0 /path/to/image.jpg)

curl http://127.0.0.1:8083/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"qwen3-vl\",
    \"messages\": [
      {\"role\": \"user\", \"content\": [
        {\"type\": \"image_url\", \"image_url\": {\"url\": \"data:image/jpeg;base64,${IMAGE_B64}\"}},
        {\"type\": \"text\", \"text\": \"What's in this image?\"}
      ]}
    ]
  }"
```

### Using with astrbot

Set the LLM API endpoint to `http://127.0.0.1:8083/v1` in astrbot's config. Model name doesn't matter — the relay proxies transparently.

---

## Performance

Tested on: Ryzen 7 9700X + RTX 3060 12GB, Qwen3-VL-4B Q4_K_M

| Metric | Value |
|--------|-------|
| Model VRAM | ~2.4 GB |
| KV cache VRAM (8K ctx) | ~1.2 GB |
| Compute buffers | ~0.3 GB |
| **Total VRAM** | **~3.8 GB** |
| Cold start | ~1.5 s |
| Text generation | ~100 tok/s |
| Idle VRAM | 0 MB |

---

## Comparison

| Approach | VRAM | Setup | Flexibility |
|----------|------|-------|-------------|
| **This relay** | On-demand ✅ | One command | Full control |
| Ollama resident | Always allocated | Simple | Limited params |
| Manual llama-server | Always allocated | Manual start/stop | Full control |
| vLLM | Always + overhead | Complex | Production-grade |

---

## License

MIT — use, modify, distribute freely.

---

## Credits

- [llama.cpp](https://github.com/ggml-org/llama.cpp) — local inference engine
- [Qwen3-VL](https://github.com/QwenLM/Qwen3-VL) — vision-language model
- [Huihui-Qwen3-VL abliterated](https://huggingface.co/noctrex/Huihui-Qwen3-VL-4B-Instruct-abliterated-GGUF) — uncensored GGUF
