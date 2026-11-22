# 从零到第一次 TTS 回调

在 Apple Silicon Mac 上运行 Local Model Station 和已可用的 DSH Web。用 `qwen3-tts` 生成一条 WAV，结束当前 DSH 轮次，再由工作台通知**同一个会话**验收。这个教程使用同机部署和回环地址。已经有可执行的 `qwen3-tts` profile 时，可直接从第 2 步开始。

## 1. 安装工作台和 TTS 模型

准备 Python 3.12、Git 和 FFmpeg。工作台与推理依赖分别安装在独立环境。

```sh
git clone https://github.com/Jamorx/local-model-station.git
cd local-model-station
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .

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

## 3. 从 DSH 会话提交任务

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
