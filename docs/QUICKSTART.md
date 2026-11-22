# 快速开始

本指南使用同一台 Apple Silicon Mac 和回环地址。先启动 Local Model Station，安装一个生成模型，再用 DSH Web 跑通「提交任务 → 执行 → 唤醒原会话」。已有工作台和可执行的 `qwen3-tts` profile 时，可以从第 2 步开始。

## 1. 安装工作台并启用队列

准备 Python 3.12、Git 和 FFmpeg。先安装工作台，启动默认的回环服务。

```sh
git clone https://github.com/Jamorx/local-model-station.git
cd local-model-station
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .

.venv/bin/python -m model_gateway serve
```

打开 [工作台](http://127.0.0.1:8777/) 确认页面能加载。默认启动只接收任务。先按 `Ctrl+C` 停止这个新启动的实例，安装首个推理环境和模型。已有实例可以沿用原有的服务和配置。

```sh
export MEDIA_LAB_ROOT="$HOME/LocalModelLab"
python3.12 -m venv .venv-tts
.venv-tts/bin/python -m pip install 'mlx-audio==0.6.3' 'huggingface-hub==1.33.0'
export MEDIA_RUNNER_PYTHON="$PWD/.venv-tts/bin/python"

model=Qwen3-TTS-12Hz-1.7B-Base-8bit
.venv-tts/bin/hf download "mlx-community/$model" \
  --local-dir "$MEDIA_LAB_ROOT/models/$model"

.venv/bin/python -m model_gateway doctor
```

在 `doctor` 输出里查看 `qwen3-tts` 的引擎和权重路径。只跑本教程时，其他 profile 可以尚未配置。该 profile 的默认调度门槛为 **6 GiB 可用内存**。已有同名完整权重时可以跳过下载。[MLX-Audio](https://github.com/Neywa-Labs/mlx-audio) · [模型权重](https://huggingface.co/mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit)

## 2. 让工作台能通知 DSH Web

在任务目录的父目录创建 `dsh-web.json`。默认文件路径为 `~/Library/Application Support/LocalModelGateway/dsh-web.json`。已有同名文件时检查其中的地址。

```json
{
  "url": "http://127.0.0.1:3443",
  "result_base_url": "http://127.0.0.1:8777"
}
```

`url` 填工作台能访问的 DSH Web 实际监听地址。`result_base_url` 填会话验收产物时能访问的工作台地址。若 DSH Web 需要认证，在启动工作台的环境中设置 `DSH_WEB_COOKIE`。局域网及 Codex 的配置见 [API 文档](API.md#服务端批次回调)。

使用第 1 步的终端启动工作台，或在已有实例上确认队列已经启用。

```sh
.venv/bin/python -m model_gateway serve --execute
```

打开 [工作台](http://127.0.0.1:8777/)，确认右上角显示「队列启用」。

## 3. 提交第一条音频任务

打开一个 DSH Web 会话，粘贴这一句话。

> 向 http://127.0.0.1:8777/api/v1/tasks 提交一条 Gateway Task v1 音频任务，profile 用 qwen3-tts，朗读内容为「你好，这是一条语音完成通知测试。」提交后结束会话，任务结束后唤醒当前会话进行校验。

DSH 代理从环境读取当前的 `DSH_SESSION_ID`，提交 `kind: audio`、`profile: qwen3-tts` 的任务，将朗读内容放在 `inputs.prompt`。提交成功后，通过 `POST /api/v1/callbacks` 登记 `target: dsh-web`、任务 ID 列表和原会话 ID。提示词可配合[仓库内的 DSH Skill](../skills/local-model-station/SKILL.md)使用。未安装 Skill 时，可让代理先读取工作台的 `/api/help.md`。

收到回调登记的 HTTP `202` 和响应字段 `id` 后，DSH 就可以结束本轮。工作台在后台等待，期间无需调用 LLM。若文本引擎配置了空闲卸载，LLM 可按该策略释放内存，下一轮验收时再按需加载。

## 4. 确认原会话收到结果

1. 在工作台确认任务完成并生成 WAV。
2. 确认原 DSH 会话收到结果，并在新一轮检查 WAV 能否读取。可以检查 RIFF/WAVE 文件头，再打开产物试听。
3. 需要核对投递时，用登记返回的 `id` 查询 `GET /api/v1/callbacks/{id}`。`delivered` 表示 DSH Web 已接收消息，最终验收结果以原会话回复为准。

| 现象 | 检查项 |
| --- | --- |
| 任务一直排队 | 确认队列启用，并检查可用内存是否达到 profile 的 6 GiB 门槛 |
| TTS 执行失败 | 核对 `MEDIA_RUNNER_PYTHON` 和权重目录，查看任务错误 |
| 回调登记返回 `403` | 确认请求来自工作台所在主机的回环地址，或按 API 文档配置局域网回调密钥 |
| 回调登记返回 `503` | 核对配置文件路径、JSON 和当前会话 ID |
| 回调显示 `uncertain` | 先检查原会话。工作台不会自动重复开启验收轮次 |

同一次提交因网络问题重试时，复用原来的 `Idempotency-Key`。任务已提交且只有回调登记失败时，保留任务 ID，只重试登记。

## 进阶复现：AI 视频自动化抽卡

先用上面的音频任务确认 DSH 能收到回调，再切换到视频。示例是一台 96 GiB Apple Silicon Mac、运行在 oMLX 的分镜／验收 LLM，以及工作台内置的 MiniMax H3 视频 profile（`h3`）。

1. **配置 LLM 空闲卸载。** 打开 oMLX 管理面板 → **模型** → 选择 DSH 实际使用的 LLM → **模型设置**，将该模型的空闲卸载 TTL（`ttl_seconds`）设为 **180 秒**，保存并确认该模型未固定常驻。等待期间避免其他客户端请求同一模型，否则空闲时间会重新计算。这是 oMLX 的单模型配置，由 oMLX 卸载并释放内存。[oMLX 模型设置](https://github.com/jundot/omlx/blob/main/omlx/model_settings.py)
2. **准备视频引擎。** 按上游的 [h3.c 安装说明](https://github.com/antirez/h3.c)构建引擎并准备 [MiniMax-H3 权重](https://huggingface.co/MiniMaxAI/MiniMax-H3)。把可执行文件放在 `$MEDIA_LAB_ROOT/h3-bin/h3`，把对应权重放在 `$MEDIA_LAB_ROOT/models/MiniMax-H3/`。用 `.venv/bin/python -m model_gateway doctor` 确认 `h3` profile 所需路径齐全，工作台队列已启用。该 profile 默认需要 **13 GiB 可用内存**。[引擎接入](ADAPTERS.md)
3. **提交并交还内存。** 先用两个短镜头试跑，再扩展到 8 个镜头的 60 秒成片。把下面提示词发给 DSH。看到任务 ID 和回调 ID 后让本轮结束。在 oMLX 活跃模型列表查看空闲倒计时和卸载；在工作台观察任务状态。若最初余量低于 13 GiB，任务应先排队，实际可用内存达标后才能开跑；余量已够时，视频可能提前开始。
4. **检查返修循环。** 整批进入终态后，工作台唤醒原 DSH 会话。LLM 在本轮按需重新加载。记录通过镜头的任务 ID 和产物链接，只重提需要返修的镜头，并用新任务 ID 登记下一批回调。若 DSH 的模型无法看视频，可由用户看片后提供逐镜验收结果。

> 为一条 60 秒短片规划 8 个镜头，注明每个镜头的动作和验收条件。先选两个短镜头，用工作台的 `h3` profile 分别提交视频任务并为这批任务登记当前 DSH 会话回调，然后结束本轮。回调后逐镜检查动作是否符合分镜，保留通过镜头，只改写失败镜头的提示词并重新提交、登记回调。每次返回任务 ID、回调 ID 和已通过产物链接。

[查看完整视频案例](../README.md#应用场景60-秒短片的抽卡与返修)
