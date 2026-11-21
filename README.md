# Local Model Station

部署在 Apple Silicon Mac 上的局域网模型任务工作台。项目的核心是把图像、视频、语音生成任务集中到一台 Mac，按模型的内存需求和系统当前可用内存调度，由局域网设备通过 API 提交任务和取回结果。

> **公开版范围**：当前 GitHub 源码提供 Gateway Task v1 协议校验和 Splash 只读监控。下方队列、局域网接口和模型表记录的是现有完整部署能力。完整调度器、下载执行器、模型适配器和生产控制台尚未随公开版分发。

## 模型任务队列与内存调度

- **一台 Mac 服务多台设备。** 同一局域网内的桌面应用、脚本和 Agent 可以通过 HTTP 提交图像、视频、语音任务，并轮询任务状态、读取日志和下载产物。模型与权重安装在执行端。
- **队列持久化。** 任务输入、状态和执行配置会保存在服务端。控制台与 API 使用同一队列，支持调整顺序、暂停后续调度、取消和重试。
- **按可用内存调度。** 每个模型 profile 配置 `available_gib_required`。单个媒体 worker 按队列顺序挑选满足当前系统可用内存门槛的任务。内存不足的任务继续等待，后续符合条件的任务可以先跑。等待超时会标记失败。执行时继续监控可用内存和交换空间增长，触发阈值时终止任务。配置门槛是调度依据，实际峰值取决于模型和输入。

### 局域网调用

完整工作台部署在 Apple Silicon Mac 上后，可绑定 `0.0.0.0:8777`。客户端使用 **执行端 Mac 的局域网 IP**，例如 `http://192.168.1.50:8777`。以下为完整工作台的原生 API 流程。

| HTTP 接口 | 用途 |
|---|---|
| `GET /api/models` | 查询模型 profile、引擎和内存门槛 |
| `POST /api/v1/tasks` | 提交任务，可用 `Idempotency-Key` 安全重试提交 |
| `GET /api/v1/tasks/{id}` | 查询排队、运行、失败或完成状态 |
| `GET /api/v1/tasks/{id}/artifacts/{index}` | 下载完成任务的指定产物 |

设置 `MEDIA_API_KEY` 后，任务接口使用 `Authorization: Bearer <key>`。绑定局域网地址的完整工作台适用于受信任的网络。

### 完整部署已接入的引擎与模型

| 用途 | 引擎 | 模型与 profile |
|---|---|---|
| 图片 | iris-metal | FLUX.3-Image-Klein（`flux3`） |
| 图片 | mflux | Z-Image-Turbo（`zimage`）、MOSS-Image-SFT（`moss`） |
| 图片编辑 | moss-edit-mps | MOSS-Image-SFT（`moss-edit`） |
| 图片 | h3-image-metal | MiniMax-H3-Image（`h3-image`） |
| 视频 | h3-c | MiniMax-H3（`h3`） |
| 视频与重制 | vpipe | MiniMax-H3（`vpipe-h3`、`vpipe-2k`） |
| 语音 | qwen3-tts-mlx | Qwen3-TTS-12Hz-1.7B-Base-8bit（`qwen3-tts`） |
| 文本转发 | OpenAI 风格 Chat Completions | 默认指向 oMLX，可指定 Splash 等上游，模型 ID 由部署配置指定 |


这里的「已接入」指在完整部署中已有 profile 或适配器。运行前需要安装对应引擎和权重。当前公开预览不会启动这些引擎。

### 可接入的模型和引擎

完整工作台的 profile 将模型权重、执行引擎、参数和内存门槛绑定在一起。自定义脚本可用 Gateway Task v1 读取 `task.json`，同步执行模型，再写出 `result.json` 和产物。可按该契约编写 Diffusers 或 MLX Audio 适配器，例如为 SDXL、Wan 或其他语音模型注册 profile。具体能力取决于运行时和权重。

- **文本引擎**可按 OpenAI 兼容接口对接 oMLX、Splash、llama.cpp 或 Ollama。文本转发与媒体队列独立运行。
- **ComfyUI** 可通过固定工作流接入。适配器需要管理提交、任务 ID、产物回收和取消。目前没有现成的 ComfyUI 执行适配器。

模型下载与 Splash 状态、指标和日志属于辅助功能。模型及引擎各自适用上游许可证。

## 运行当前公开预览

需要 macOS 和 Python 3.12+。Splash 由你自行安装和启动。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
local-model-station
```

打开 [API 浏览器](http://127.0.0.1:8777/docs)。与已有工作台并行运行时可指定其他端口，例如 `local-model-station --port 18777`。

| 入口 | 用途 |
|---|---|
| `GET /health` | 检查监控服务 |
| `GET /api/v1/engines/splash` | 获取本机实例就绪状态、原生指标和可读取的历史 |
| `POST /api/v1/tasks/validate` | 检查请求是否符合 Gateway Task v1 结构 |

公开预览只绑定回环地址。状态与日志取自本机，可用环境变量包括：

- `MEDIA_SPLASH_PORT`，用于给独立实例的请求日志匹配模型，以及限定可接收指标密钥的端口。
- `MEDIA_SPLASH_API_KEY`，可选的 Splash 指标端点 Bearer 密钥。
- `MEDIA_SPLASH_LOG`，独立 Splash 请求日志的绝对路径。
- `MEDIA_LAB_ROOT`，可选的既有运行目录，默认 `~/LocalModelLab`。如果目录里有 oMLX 统计和日志，监控可只读展示。

Splash 可独立运行。监控采样通过引擎的只读端点，推理客户端直接连接该引擎。

## 许可证

公开代码采用 [Apache License 2.0](LICENSE)。推理引擎、第三方 Python 依赖和模型权重适用各自的许可证。
