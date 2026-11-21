# 新模型与引擎接入

首期 macOS。新接入统一遵循 [Gateway Task v1](GATEWAY_TASK_V1.md)。推荐先写最小脚本，再注册配置，最后做小尺寸 / 短时长实测。

## 四个概念

| 概念 | 示例 | 谁负责 |
|---|---|---|
| model_id | MiniMax-H3 | 权重身份 |
| engine | h3-c、vpipe | 加载与执行实现 |
| profile ID | h3、vpipe-2k | 请求中的 model；绑定默认参数与资源要求 |
| protocol | 原生 API、火宝 OpenAI 图片、MiniMax 视频 | 请求 / 响应映射 |

H3 的 h3.c 直接生成与 vpipe 的底片 2K 重制不是可互换功能。换引擎时必须重新检查输入、输出、时长和精度支持，不能只改名称。当前内置 backend 封装既有引擎，重型依赖独立安装。

## 脚本适配器

复制 `config/profiles.json` 为 `config/profiles.local.json`，保留仍需使用的原配置，然后添加引擎与 profile。注册表目前整体替换，不做文件合并。

```json
{
  "version": 1,
  "engines": {
    "custom-tts": {
      "type": "script",
      "contract": "gateway.task.v1",
      "argv": ["/absolute/path/to/venv/bin/python", "{config_dir}/../examples/my_tts.py", "{job_dir}"],
      "required_paths": ["{config_dir}/../examples/my_tts.py"]
    }
  },
  "profiles": {
    "my-tts": {
      "model_id": "my-tts-weights-v1",
      "engine": "custom-tts",
      "kind": "audio",
      "directory": "MyTTS",
      "available_gib_required": 6,
      "options": {"voice": {"type": "string", "required": true}}
    }
  }
}
```

设置 `MEDIA_PROFILES_FILE` 指向该文件。`directory` 相对 `MEDIA_LAB_ROOT/models`，也可为绝对路径；无权重依赖时留空。`available_gib_required` 为整机可用内存门槛，应通过本机实测确定。

argv 只接受 `{python}`、`{config_dir}`、`{job_dir}` 占位符，使用参数数组直接启动，无 shell。prompt 等用户输入从 task.json 读取；不要将它拼到 shell 命令。配置是管理员信任的代码执行边界，不向客户端提供上传脚本或修改注册表接口。

新增脚本读取 task.json（Gateway Task v1）；原 input.json 仅作为旧适配器兼容桥。脚本读取：

- `task.json`：规范化请求，inputs.prompt / inputs.assets / parameters / options；素材是任务内相对路径。
- `execution.json`：模型目录、引擎配置、profile、注册版本与运行根目录。

脚本写入：

```python
from pathlib import Path
import json, sys
job = Path(sys.argv[1])
task = json.loads((job / "task.json").read_text())
params = task["parameters"]
prompt = task["inputs"]["prompt"]
execution = json.loads((job / "execution.json").read_text())
# 使用 execution["model_dir"] 与 params 调用本地模型，写 output.wav。
(job / "result.json").write_text(json.dumps({"schema_version":"gateway.result.v1", "artifacts":[{"path":"output.wav", "kind":"audio"}]}))
```

结果文件必须在当前任务目录内。可选写 `stage.json`（例如 `{"schema_version":"gateway.progress.v1","stage":"loading"}`）；stdout/stderr 自动保存为 run.log。成功退出且结果通过路径 / 后缀 / 非空检查才算成功；检查不等于内容质量审核。

脚本必须同步等待子进程完成，不能启动脱离进程组的后台推理。取消、超时、内存保护会终止整个进程组。失败用非零退出码。参考完整可运行实现 `examples/macos_say.py`；这是供适配器开发参考的系统 TTS 脚本。默认模型目录注册 Qwen3-TTS 语音 profile。

`options` 支持 string/number/integer/boolean、required、enum、minimum/maximum。不要将密钥放入配置 env 或 options：执行快照可通过任务 API 查询。密钥应由运行环境或引擎自己的本地密钥存储提供。

## 接入验收顺序

1. `python -m model_gateway doctor` 检查路径。
2. `/api/validate` 检查请求与能力匹配。
3. 隔离 `MEDIA_JOBS_DIR` 与端口，执行最小真实任务。
4. 验证输出可读、失败可定位、取消停止进程、重试保留输入、内存估算合理。
5. 空闲时重启生产服务，再让调用方使用新 profile ID。

需要多模态输入时，在 profile 中声明 `input_roles`，例如 `["reference_audio"]`；从 task.inputs.assets 读取素材。统一解码、保存、文件校验和引用复制由网关负责，模型特有的素材组合约束仍需适配器校验。

配置快照冻结配置内容，不冻结脚本、Python 包、权重和二进制。需要严格复现时使用不可变版本目录并记录模型哈希。内置引擎部分辅助模型路径仍按既有环境布局，移动辅助权重前需核查对应 builtin_runner / helper。

## 已接入的本地 Qwen3-TTS Base

`qwen3-tts` profile 使用 `qwen3-tts-mlx` 脚本引擎，直接加载本地 `Qwen3-TTS-12Hz-1.7B-Base-8bit`。运行 Python 需安装 mlx-audio 及模型所需依赖；不会自动下载权重。推理在网关进程组内同步完成，取消会停止本地推理，无需新增端口或转交 8000 后台请求。

原生请求：`kind: audio`、`profile: qwen3-tts`、`inputs.prompt` 为朗读原文。可选一个 `reference_audio` 素材，必须配 `options.ref_text` 精确转录；其余选项仅 `temperature`（0–1.5）及 `max_tokens`（32–2048）。无参考时生成新合成音色，有参考时复用音色。生成结果是 WAV，不是已通过人类听审的交付物。

该 Base 模型不支持 instructions、speed、预设 voice；未知 options 会拒绝。通用 parameters.seed 不用于此引擎，也不承诺逐字节确定性。已实测无参考音频生成及三条参考克隆，成片技术验证通过；自然度仍待人工听审。6 GiB 是保守预检门槛，非实测峰值保证。

## 尚未实现

通用异步 HTTP 引擎、远端任务持久化映射与取消对账尚未实现。不要在脚本里“发出远端任务就退出”来模拟成功：本地取消无法保证远端停止。后续 HTTP 适配器必须持久化 upstream ID、处理提交响应丢失，禁止盲目重发。

运行中挂起默认关闭。只有隔离验证后才声明 `supports_process_pause: true`，语义与验证范围见 [队列控制](QUEUE_CONTROL.md)。
