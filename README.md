# Local Model Station

简体中文 · [English](README.en.md)

![Local Model Station：手机等局域网设备提交任务，经内存调度与本地执行，完成后回调原会话](docs/images/local-model-station-hero.png)

面向 Apple Silicon 的本地模型工作台，提供图像／视频／音频任务管理、按内存余量调度队列、批次任务回调、模型下载和 Splash 引擎看板，默认端口为 **8777**。

**首期仅支持 macOS。** 本地 MLX / Metal / MPS 生成主要面向 Apple Silicon；Intel Mac、Windows、Linux 暂不在支持范围。当前为 0.1 开发版，项目自有代码按 Apache-2.0 发布。推理引擎和模型权重需自行安装。

[快速开始](docs/QUICKSTART.md)包含安装、首条任务、回调验收和视频抽卡复现步骤。

## 功能

- **任务管理**：在局域网的手机或电脑上打开工作台，提交任务、调整队列顺序、暂停后续调度、取消或重试任务，查看执行状态和日志。也可通过 HTTP API 调用。
- **输入与产物管理**：按任务查看提示词、参数和参考素材。编辑历史任务并重新提交时可复用原素材，无需重新上传。完成后按任务查看或下载产物。
- **任务回调**：整批任务完成后，自动唤醒原 DSH Web 或本机 Codex 会话，交付产物链接与失败摘要，继续验收。
- 支持 OpenAI 风格 Chat Completions 文本转发，也可独立观察 Splash。
- 支持图片、视频和音频任务，默认语音 profile 为 Qwen3-TTS。
- 不提供候选评审、制作阶段流转或多 Agent 编排。Phosphene 暂无通用后端接入承诺。

## 应用场景：60 秒短片的抽卡与返修

**解决的问题：同一台 Mac 上，高质量 LLM 和视频／图像模型可能无法同时驻留。** 以一台 96 GiB Apple Silicon Mac 为例，假设跑着用于分镜和验收的大模型，系统只剩 12 GiB 可用内存。本项目的 MiniMax-H3 视频 profile 要求至少 13 GiB，FLUX.3 图像 profile 要求至少 28 GiB，此时两类任务都要等。12 GiB 是示例余量，13／28 GiB 是配置的启动门槛。

**实际怎么跑：** DSH 把 60 秒短片拆成 8 个镜头，提交候选视频并登记回调，然后结束本轮。在 oMLX 中打开 DSH 所用 LLM 的**模型设置**，将空闲卸载 TTL（`ttl_seconds`）设为 **180 秒**。该 LLM 空闲满 3 分钟后，由 oMLX 自动卸载并释放内存。8777 检测到可用内存达到门槛后启动视频任务。整批结束后，回调唤醒原 DSH 会话，文本模型按需重新加载，逐镜查看候选结果。

在工作台按镜头查看任务的提示词、参考素材、执行日志和生成视频。返修时可以打开历史任务编辑重试，沿用原有素材。

例如首轮 6 个镜头通过，另有 2 个镜头人物动作偏离分镜。DSH 保留通过片段，只改这 2 个镜头的提示词或参考素材并重提，再次结束本轮。下一次回调回来继续验收，直到可按分镜剪辑成片。这样，**分镜与验收用 LLM，生成阶段用视频模型，两边轮流使用同一台 Mac 的内存。**

按[快速开始中的视频抽卡步骤](docs/QUICKSTART.md#进阶复现ai-视频自动化抽卡)复现模型设置、首批提交和回调返修。

![AI 视频自动化抽卡泳道：用户提出短片需求，DSH 逐镜验收，8777 生成及回调，问题镜头进入返修循环](docs/images/video-candidate-loop.png)

## 核心能力：任务回调

**把长时间生成任务交给 8777，等整批结束后再唤醒原会话。** DSH Web 或本机 Codex 可以提交图像、视频、音频任务，并登记完成回调，然后结束当前轮次。8777 负责按内存余量调度和等待。等待期间没有文本推理请求，文本模型可以按引擎的空闲策略卸载，给本机生成任务让出内存。

1. **提交**。调用 `POST /api/v1/tasks`，记录这批任务的 ID。
2. **登记**。调用 `POST /api/v1/callbacks`，传入 `task_ids`、`target` 和原会话的 `session_id`。回调登记持久化。同一批任务对同一个目标会话重复登记会返回同一个通知 ID。
3. **验收**。全部任务到达终态后，8777 一次性投递每项任务的状态、产物链接和失败摘要。DSH Web 把消息排入原聊天，Codex 由本机 CLI 恢复原会话。代理可以依照提交时的要求检查产物、报告失败项并继续后续工作。

### 回调接口

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/v1/callbacks` | 登记一批任务完成后需要唤醒的会话，返回 `202` 和回调 ID |
| `GET` | `/api/v1/callbacks/{id}` | 查询回调状态及关联的任务 ID |

以下示例在 8777 所在主机运行。`TASK_ID` 是已提交的任务 ID，`DSH_SESSION_ID` 是当前 DSH Web 会话 ID。

```bash
curl -fsS http://127.0.0.1:8777/api/v1/callbacks \
  -H 'Content-Type: application/json' \
  -d "{\"task_ids\":[\"$TASK_ID\"],\"target\":\"dsh-web\",\"session_id\":\"$DSH_SESSION_ID\"}"

curl -fsS "http://127.0.0.1:8777/api/v1/callbacks/$CALLBACK_ID"
```

第一个请求返回的 `id` 用于第二个请求的 `CALLBACK_ID`。本机 Codex 使用 `target: codex` 和 Codex 会话 UUID。局域网登记与查询需要 `X-DSH-Callback-Key`，若启用了 API 鉴权还需 Bearer 密钥。

**回调送达与产物验收是两个状态**。例如 DSH Web 接收消息后，原会话还需执行验收。若 Codex 会话存放在另一台设备，可选用独立等待器。[配置与回调 API](docs/API.md#服务端批次回调)

在已配置 DSH skill 的会话里，可以直接说：

> 用 qwen3-tts 生成这段语音。提交后结束本轮，等任务完成后唤醒当前会话，检查 WAV 能否读取并给我产物链接。

### DSH Web 和 8777 在不同局域网设备

**两边可互访时，无需启动额外进程。** DSH 所在设备提交任务并登记回调，8777 自己等待整批结束，再回投给托管原会话的 DSH Web。浏览器所在设备可以与两者都不同。

1. 在 8777 的任务目录父级配置 `dsh-web.json`。默认路径是 `~/Library/Application Support/LocalModelGateway/dsh-web.json`。例如 8777 位于 `192.168.1.50`，DSH Web 位于 `192.168.1.60`：

   ```json
   {"url":"http://192.168.1.60:3443","result_base_url":"http://192.168.1.50:8777"}
   ```

   `url` 是 8777 能访问的 DSH Web 地址，`result_base_url` 是 DSH 能访问的 8777 地址。DSH Web 需要通过自己的配置接受来自 8777 的连接。若实例要求 Cookie，由管理员配置 `DSH_WEB_COOKIE` 到 **8777 服务进程**的环境，使用该实例的正常认证方式。

2. 在 **8777 服务进程**和**提交端**分别配置同一个 `MEDIA_DSH_CALLBACK_KEY`。脚本会用 `X-DSH-Callback-Key` 发送它。若 8777 同时启用了普通 API Bearer 鉴权，提交端也需设置 `MEDIA_API_KEY`。这些值通过本机环境或服务管理器提供。使用不受信任的网络时应通过 HTTPS 或可信内网隧道访问。

3. 提交任务后，从 DSH 当前会话运行 [一次性登记脚本](scripts/register_dsh_callback.py)。它读取 `DSH_SESSION_ID`，也可传入明确的 `--session-id`。拿到回调 ID 后即可结束当前轮次。脚本只做登记，随即退出。

   ```sh
   python3 scripts/register_dsh_callback.py --url http://192.168.1.50:8777 "$TASK_ID_1" "$TASK_ID_2"
   ```

**只有反向访问不通时才需要等待进程。** 例如 DSH Web 仅监听本机，且不打算让 8777 连接它，可在 DSH 所在设备运行 [批次等待器](model_gateway/batch_wait.py)。它主动访问 8777，整批完成后再调用本机 DSH Web，等待阶段无需运行模型。

```sh
python3 batch_wait.py --url http://192.168.1.50:8777 "$TASK_ID_1" "$TASK_ID_2" \
  --resume-agent dsh-web --dsh-web-url "$LOCAL_DSH_WEB_URL" \
  --detach --log "$LOG_PATH" --receipt "$RECEIPT_PATH"
```

当前服务端配置对应一个 DSH Web 目标。异机部署示例供有需要的用户自行验证。

## 控制台截图

![工作总览，包含任务队列和本机资源](docs/screenshots/overview.png)

<details>
<summary>查看任务队列和模型目录</summary>

![任务队列与任务操作](docs/screenshots/task-queue.png)

![模型目录，包含引擎、profile 和内存门槛](docs/screenshots/model-catalog.png)

</details>

## 模型任务队列与内存调度

- **一台 Mac 服务多台设备。** 同一局域网内，手机和电脑可用浏览器打开控制台，提交图像、视频、语音任务，查看进度和下载产物。桌面应用、脚本和 Agent 也可通过 HTTP API 调用。模型与权重安装在执行端。
- **队列持久化。** 任务输入、状态和执行配置会保存在服务端。控制台与 API 使用同一队列，支持调整顺序、暂停后续调度、取消和重试。
- **按可用内存调度。** 每个模型 profile 配置 `available_gib_required`。单个媒体 worker 按队列顺序挑选满足当前系统可用内存门槛的任务。内存不足的任务继续等待，后续符合条件的任务可以先跑。等待超时会标记失败。执行时继续监控可用内存和交换空间增长，触发阈值时终止任务。配置门槛是调度依据，实际峰值取决于模型和输入。

### 局域网调用

工作台部署在 Apple Silicon Mac 上后，可绑定 `0.0.0.0:8777`。客户端使用 **执行端 Mac 的局域网 IP**，例如 `http://192.168.1.50:8777`。以下为原生 API 调用流程。

| HTTP 接口 | 用途 |
|---|---|
| `GET /api/models` | 查询模型 profile、引擎和内存门槛 |
| `POST /api/v1/tasks` | 提交任务，可用 `Idempotency-Key` 安全重试提交 |
| `GET /api/v1/tasks/{id}` | 查询排队、运行、失败或完成状态 |
| `GET /api/v1/tasks/{id}/artifacts/{index}` | 下载完成任务的指定产物 |

设置 `MEDIA_API_KEY` 后，任务接口使用 `Authorization: Bearer <key>`。绑定局域网地址的完整工作台适用于受信任的网络。

## 从源码运行

需要 macOS、Python 3.12、FFmpeg，以及单独安装的模型权重和引擎。Python 发行包名为 `local-model-gateway`，CLI 为 `lmg`。API 环境与推理环境可分开；安装 API 依赖不会下载模型。首期使用源码 / editable 安装，尚未发布 wheel 或 Homebrew 包。

```sh
git clone https://github.com/Jamorx/local-model-station.git
cd local-model-station
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
export MEDIA_LAB_ROOT="$HOME/LocalModelLab"
export MEDIA_RUNNER_PYTHON="$HOME/LocalModelLab/venv/bin/python"
python -m model_gateway doctor
python -m model_gateway serve
```

默认监听 `127.0.0.1:8777`，**仅入队、不执行**。确认引擎环境后启用生成并开放局域网：

```sh
python -m model_gateway serve --host 0.0.0.0 --port 8777 --execute
```

控制台 `http://localhost:8777/`，交互式 API `http://localhost:8777/docs`，协议 Schema `/openapi.json`。无密钥时信任局域网客户端；设置 `MEDIA_API_KEY` 后，所有任务 API 都需要 Bearer。页面目前适用于可信局域网免密模式，不提供登录界面。

默认数据目录是 `~/Library/Application Support/LocalModelGateway/jobs`，与源码、权重分离。只运行一个 worker；相同任务目录有进程锁。

## 调用

```sh
python -m model_gateway models
python -m model_gateway validate examples/task-v1.json
python -m model_gateway submit examples/task-v1.json --key task-v1-demo-001
python -m model_gateway validate examples/image.json
python -m model_gateway submit examples/image.json --key image-demo-001
python -m model_gateway status JOB_ID
python -m model_gateway draft JOB_ID > edited-task.json
# 编辑 edited-task.json 后以新幂等键提交，素材无需重新上传。
python -m model_gateway cancel JOB_ID
python -m model_gateway retry JOB_ID --key image-demo-retry-001
```

任务提交返回 202 后需轮询状态；成功后读取 `/api/jobs/JOB_ID/output`。同一次网络重试复用幂等键，新一次生成使用新键。

```sh
curl -X POST http://127.0.0.1:8777/api/jobs \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: image-demo-002' \
  --data-binary @examples/image.json
```

## 模型与引擎接入

`config/profiles.json` 区分 `model_id`（权重身份）、`engine`（实现）、profile ID（调用配置）。原有 `zimage`、`h3` 等调用名称保留。新增脚本引擎通过 argv 读取 `task.json`（Gateway Task v1）、写入统一 `result.json`，无需修改调度器。

提交时冻结模型目录、引擎配置、注册表版本和特定工具路径，保存在 `execution.json`；重试复用该快照。**代码、二进制和权重文件本身不会被复制或锁定**，更换它们仍会影响复现。修改配置后需在队列空闲时重启，无热重载。

### 已注册的引擎与模型

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

注册项是否可运行取决于本机引擎、权重与资源。查看 `/api/models` 和对应 probe。引擎及模型权重各自适用其许可证。

### 可扩展的引擎与模型

同步脚本适配器可接入 Diffusers、MLX Audio 或其他能读取任务、写出结果的运行时。候选包括 Diffusers 支持的 SDXL、Wan，以及 MLX Audio 支持的其他语音模型。文本可通过 OpenAI 兼容接口对接 Splash、llama.cpp、Ollama 等。每个候选都需按具体模型和运行时版本验证输入、输出、内存、流式响应及取消语义。[适配器契约](docs/ADAPTERS.md) · [Splash 直连与管理方案](docs/SPLASH.md)

**ComfyUI 可以接入。** 建议由管理员注册 API 格式的固定工作流，每个工作流声明模型、可映射的节点输入和输出类型。网关通过 `POST /prompt` 提交，记录 `prompt_id`，按 `GET /history/{prompt_id}` 查询并通过 `GET /view` 收回产物。SDXL、FLUX、Wan 等候选取决于实际节点、工作流和权重。取消、断线重试与远端恢复需单独实现，尤其要避免在共享实例上用全局中断取消别人的任务。当前尚无 ComfyUI 执行适配器。

左侧「Splash 管理」提供模型配置、实时就绪状态、oMLX 历史用量和近期 Splash 请求日志。[状态接口和配置说明](docs/SPLASH.md#8777-只读视图)

### 模型下载

打开左侧「模型下载」，即可搜索 Hugging Face 仓库、下载入队、查看进度和历史、暂停、续传、重探和移除。下载执行器和 worker 随网关源码分发。移除任务保留文件。新任务写入 `MEDIA_LAB_ROOT/models`，完成后仍需按推理引擎注册 profile。

下载控制由 `/api/v1/downloads` 提供。发行版默认在 `local.model-gateway` LaunchAgent 中启动下载执行器，直接运行时设置 `MEDIA_DOWNLOADS_ENABLED=1` 可启用。下载与媒体推理队列各自运行。支持检测 HF Mirror 与 Hugging Face 官方的 HTTP 连通性。自动模式优先 HF Mirror，新任务也可手动选源。已有任务可暂停、切换下载源后续传，沿用本地记录的仓库 revision。可通过 `MEDIA_HF_ENDPOINT` 覆盖镜像地址。旧 8767 的任务、历史、校验与续传记录可在同一个 `MEDIA_LAB_ROOT` 直接复用。应先停止旧下载守护进程，再启动新版 8777。

已移除 `/legacy` 和 `/legacy/manage`，统一控制台位于 `/`。

- [队列控制、暂停与恢复](docs/QUEUE_CONTROL.md)
- [Gateway Task v1 统一协议](docs/GATEWAY_TASK_V1.md)
- [API 与错误处理](docs/API.md)
- [新增模型 / 引擎](docs/ADAPTERS.md)
- [macOS 安装、后台服务与迁移](docs/OPERATIONS.md)
- [兼容协议与边界](docs/COMPATIBILITY.md)
- [验收记录](docs/VALIDATION.md)
- [后续 TODO](docs/TODO.md)
- [贡献指南](CONTRIBUTING.md)、[安全范围](SECURITY.md)、[第三方来源](THIRD_PARTY_NOTICES.md)

## 控制台前端

控制台使用 Bootstrap 5.3.9 的表单、按钮、卡片和导航样式，以及 Tabulator 6.6.1 的下载历史与 Splash 数据表。两个依赖都随源码分发，许可证同目录保存，可在局域网离线加载。共享主题与布局在 `model_gateway/studio.css`，现有任务弹窗沿用原生 dialog。

## 目录

```text
model_gateway/      API、队列、注册表、执行器、协议、Web 控制台
config/             模型与引擎注册表；*.local.json 不提交
examples/           请求 JSON、最小 TTS 脚本
scripts/            macOS LaunchAgent 管理
tests/             隔离协议测试与真实子进程队列测试
docs/              调用、接入、运维、验收与路线图
```

```sh
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
node --check model_gateway/studio.js
```

测试不加载真实大模型，不接触生产任务目录。CI 只跑 macOS。

## 许可证

自有代码采用 [Apache-2.0](LICENSE)。第三方代码、外部推理引擎和模型权重各自适用其许可证。详见 [第三方声明](THIRD_PARTY_NOTICES.md)。
