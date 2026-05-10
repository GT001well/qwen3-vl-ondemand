# Qwen3-VL On-Demand Relay

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue)

一个轻量级的按需加载代理，专为 Qwen3-VL（以及任意多模态 GGUF 模型）设计。

> 模型只有在收到请求时才加载到显存，空闲后自动卸载，把 GPU 内存还给其他任务。

---

## 为什么需要这个？

跑本地大模型（尤其视觉模型）有个尴尬的地方：

- 常驻显存的话，其他任务（生图、训练、游戏）就没空间了
- 每次手动启停又太麻烦
- 模型加载要好几秒，不想每次请求都等

这个 relay 的思路很简单：**常驻一个几乎不占资源的代理进程，真正的模型按需加载**。

工作流程：

```
无请求时：  relay 监听 8083 端口 ← 零显存占用
有请求时：  relay → 自动拉起 llama-server → 加载模型到显存
空闲超时：  relay → 自动杀掉 llama-server → 显存释放
```

---

## 功能特性

- **零显存常驻** — relay 本身只占几 MB 内存
- **透明的接口** — 完全透传 `/v1/chat/completions`、`/v1/models` 等所有端点，任何 OpenAI 兼容客户端都能用
- **自动生命周期** — 请求触发加载，空闲超时自动卸载
- **PDEATHSIG 保护** — relay 进程意外死亡时，llama-server 自动陪葬，不留孤儿进程
- **跨模型通用** — 换模型只需改环境变量，不限于 Qwen3-VL
- **纯 Python 标准库** — 零第三方依赖

---

## 快速开始

### 前提条件

- Python 3.8+
- [llama.cpp](https://github.com/ggml-org/llama.cpp)（需要编译 CUDA 版本）
- 一个支持多模态的 GGUF 模型 + mmproj 视觉投影文件
- NVIDIA GPU（其他后端改 `--n-gpu-layers` 等参数即可）

### 安装

```bash
git clone https://github.com/GT001well/qwen3-vl-ondemand.git
cd qwen3-vl-ondemand
chmod +x start.sh stop.sh
```

### 配置

通过环境变量配置，所有参数都有默认值：

```bash
# 模型路径
export VL_MODEL="/path/to/your/model.gguf"
export VL_MMPROJ="/path/to/your/mmproj.gguf"

# llama-server 路径
export LLAMA_SERVER="/path/to/llama-server"

# 端口
export VL_PORT=8083
export VL_INTERNAL_PORT=8084

# 空闲超时（秒），默认 300 秒 = 5 分钟
export VL_IDLE_TIMEOUT=300

# 上下文长度
export VL_CTX_SIZE=8192
```

也可以直接改 `vl-relay.py` 文件开头的常量。

### 启动

```bash
./start.sh
```

### 测试

```bash
curl http://127.0.0.1:8083/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "你好"}],
    "max_tokens": 100
  }'
```

第一次请求会等几秒（模型加载），之后就是正常响应速度。

### 停止

```bash
./stop.sh
```

或者直接关掉终端（start.sh 用的是 `exec` 模式，关终端 relay 就退出，子进程自动陪葬）。

---

## 环境变量说明

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `VL_MODEL` | `~/AI-Server/model/qwen3-vl/...gguf` | GGUF 模型路径 |
| `VL_MMPROJ` | `~/AI-Server/model/qwen3-vl/mmproj-F16.gguf` | 视觉投影器 |
| `LLAMA_SERVER` | `~/AI-Server/llama.cpp/build/bin/llama-server` | llama-server 路径 |
| `VL_PORT` | `8083` | 对外监听端口 |
| `VL_INTERNAL_PORT` | `8084` | 内部 llama-server 端口 |
| `VL_IDLE_TIMEOUT` | `300` | 空闲超时（秒） |
| `VL_POLL_INTERVAL` | `15` | 空闲检测间隔（秒） |
| `VL_CTX_SIZE` | `8192` | 上下文长度 |
| `VL_N_GPU_LAYERS` | `99` | GPU 卸载层数 |
| `VL_PARALLEL` | `1` | 并行请求槽位数 |
| `VL_CACHE_K` | `q8_0` | Key 缓存类型（f16/q8_0/q4_0） |
| `VL_CACHE_V` | `q8_0` | Value 缓存类型（f16/q8_0/q4_0） |

---

## 工作原理详解

```
┌─────────────────────────────────────────────┐
│               你的应用                        │
│  (astrbot、Open WebUI、任意 OpenAI 客户端)    │
└──────────────────┬──────────────────────────┘
                   │ POST /v1/chat/completions
                   ▼
┌─────────────────────────────────────────────┐
│           vl-relay.py（Python 代理）          │
│                                              │
│  1. 收到请求 → 检查 llama-server 是否运行      │
│  2. 未运行 → 启动 llama-server + 等待就绪      │
│  3. 透传请求到内部端口                           │
│  4. 返回响应                                    │
│  5. 重置空闲计时器                               │
│  6. 空闲超时 → 杀掉 llama-server               │
└──────────────────┬──────────────────────────┘
                   │ 代理转发
                   ▼
┌─────────────────────────────────────────────┐
│           llama-server（llama.cpp 后端）      │
│  端口 INT_PORT，占用显存                      │
│  有请求时存在，无请求时消失                     │
└─────────────────────────────────────────────┘
```

**几个关键设计：**

**PDEATHSIG 保护**：通过 Linux `prctl(PR_SET_PDEATHSIG, SIGTERM)` 确保子进程在 relay 死亡时自动退出。即使 `kill -9` 杀了 relay，llama-server 也会跟着死，不会出现没人管的进程占着显存。

**exec 启动**：`start.sh` 用 `exec python3 vl-relay.py` 替代当前 shell 进程。所以终端窗口关闭 = relay 死亡 = llama-server 陪葬。不需要 systemd service 或 nohup 之类的东西。

**透明代理**：所有 HTTP 方法（GET/POST/PUT/DELETE）原样透传，不关心具体 API 格式。所以文本对话、带图片的视觉请求、模型列表查询等全部自动支持，不用额外适配。

---

## 使用示例

### 文本对话

```bash
curl http://127.0.0.1:8083/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-vl",
    "messages": [{"role": "user", "content": "你是谁"}],
    "max_tokens": 100
  }'
```

### 视觉理解（传图片）

```bash
IMAGE_B64=$(base64 -w0 /path/to/image.jpg)

curl http://127.0.0.1:8083/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"qwen3-vl\",
    \"messages\": [
      {\"role\": \"user\", \"content\": [
        {\"type\": \"image_url\", \"image_url\": {\"url\": \"data:image/jpeg;base64,${IMAGE_B64}\"}},
        {\"type\": \"text\", \"text\": \"这张图里有什么？\"}
      ]}
    ]
  }"
```

### 接入 astrbot

在 astrbot 配置中将 LLM API 地址设为 `http://127.0.0.1:8083/v1`，模型名随意填，relay 会自动透传。

---

## 性能参考

测试环境：Ryzen 7 9700X + RTX 3060 12GB，模型为 Qwen3-VL-4B Q4_K_M

| 指标 | 值 |
|------|-----|
| 模型显存 | ~2.4 GB |
| KV cache 显存（8K 上下文） | ~1.2 GB |
| 计算缓存 | ~0.3 GB |
| **总计显存** | **~3.8 GB** |
| 冷启动时间 | ~1.5 秒 |
| 文本生成速度 | ~100 tok/s |
| 空闲时显存 | 0 MB |

---

## 和其他方案对比

| 方案 | 显存占用 | 操作复杂度 | 灵活度 |
|------|---------|-----------|--------|
| **这个 relay** | 按需加载 ✅ | 一个命令 | 全参数可调 |
| Ollama 常驻 | 一直占着显存 | 简单 | 可调参数少 |
| 手动 llama-server | 一直占着显存 | 需手动启停 | 全参数可调 |
| vLLM | 常驻 + 额外调度开销 | 配置复杂 | 生产级 |

---

## License

MIT — 随便用，随便改，随便发。

---

## 致谢

- [llama.cpp](https://github.com/ggml-org/llama.cpp) — 本地推理引擎
- [Qwen3-VL](https://github.com/QwenLM/Qwen3-VL) — 视觉语言模型
- [Huihui-Qwen3-VL abliterated](https://huggingface.co/noctrex/Huihui-Qwen3-VL-4B-Instruct-abliterated-GGUF) — 去审查版 GGUF
