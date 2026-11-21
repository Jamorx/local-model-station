# Gateway Task v1

首期 macOS。所有入口进入同一个校验、持久化和调度通道；不要为每个新模型重复实现 OpenAI / MiniMax 协议。

```text
原生 v1 / 旧原生 API / OpenAI 图片兼容 / MiniMax 视频兼容
                → 输入规范化与能力校验
                → task.json + execution.json → 单队列
                → 引擎适配器 → gateway.result.v1
```

## 身份与能力

- `profile`：选择配置，例如 h3、vpipe-2k。
- `model_id`：权重身份，例如 MiniMax-H3。
- `engine_id`：执行实现，例如 h3-c（界面显示 h3.c）、vpipe。
- `registry_revision`：提交时的注册表哈希。

h3 和 vpipe-2k 使用同一模型身份，但 h3.c 支持直接视频生成，当前 vpipe 配置需要底片做重制。它们不是功能完全相同的两个开关。profile 的 `input_roles`、参数约束和引擎 options 决定可接受输入；换配置必须重新校验，不能悄悄删除素材或参数。

## 调用方请求

`POST /api/v1/tasks`，返回 HTTP 202 和规范化任务状态。可带 `Idempotency-Key`，沿用既有幂等规则。

```json
{
  "schema_version": "gateway.task.v1",
  "profile": "h3",
  "kind": "video",
  "inputs": {
    "prompt": "镜头缓慢推进，杯口热气升起",
    "assets": [{"role": "first_frame", "data_base64": "BASE64_IMAGE"}]
  },
  "parameters": {"width": 768, "height": 768, "seconds": 5, "seed": 42},
  "options": {},
  "metadata": {"project": "demo", "shot": "SH010"}
}
```

`parameters` 是统一的生成参数，`options` 是注册表声明的引擎扩展参数。不能在 parameters 偷放 model/profile/prompt 或附件字段。未知字段、未知角色、不支持的素材组合均拒绝。

素材只能选择一个来源：`data_base64`（或 data URL）或 `source`。source 只允许引用已有任务的**输入素材**，不接受任意磁盘路径 / URL。

```json
{"role":"reference_audio","source":{"task_id":"32位任务ID","asset_id":"ref-audio-00.wav","sha256":"可选64位SHA256"}}
```

素材角色：first_frame、last_frame、input_image、edit_image、reference_image、reference_audio、reference_video、audio_condition（旧单音频字段）、draft。图片最多 4 MiB，WAV 6 MiB，MP4 32 MiB；还受每类数量、组合及模型画布限制。v1 请求最多 16 项素材，H3 最多 12 项。edit_image 在 h3-image 上最多 16 张：任务目录按 edit-image-XX.png 文件名排序依次绑定 `<Picture 1..N>`，多参考编辑在 prompt 中按编号引用。脚本引擎必须声明 input_roles；默认不接受素材。

## 引擎执行契约

每个任务目录包含：

| 文件 | 用途 |
|---|---|
| `task.json` | 已解析的 Gateway Task v1，供引擎读取 |
| `execution.json` | 冻结的配置、模型目录、解释器、注册版本 |
| 输入媒体文件 | 任务自己的副本；不会依赖源任务生命周期 |
| `stage.json` | 可选 gateway.progress.v1，原子替换更新 |
| `result.json` | 成功结果；必须先完成文件写入，再退出 0 |
| `run.log` | stdout / stderr |
| `input.json` | 旧引擎兼容桥保留的参数，不是新引擎的推荐入口 |

task.json 的 `inputs.assets` 已从 base64/source 转成任务内相对 path，包含角色、MIME、字节数、SHA-256、图片尺寸及可获得的音视频探测信息。不要把 HTTP 请求中的 source 直接交给引擎下载。引擎只访问自身任务目录和 execution 中配置的权重。

新增脚本引擎设置 `contract: "gateway.task.v1"`。执行器验证 task.json，设置 GATEWAY_TASK_FILE / GATEWAY_EXECUTION_FILE 环境变量，argv 仍可传 `{job_dir}`。使用参数数组启动，不经过 shell。

```json
{
  "schema_version": "gateway.result.v1",
  "artifacts": [{"path": "output.wav", "kind": "audio"}],
  "metrics": {"inference_seconds": 1.25},
  "details": {"sample_rate": 24000}
}
```

允许多个产物；第一个必须与请求 kind 相同。所有产物必须是任务目录内的非空文件，扩展名符合声明 kind；禁止绝对路径、目录穿越、越界符号链接。验证不等于音画质量审核。网关补充模型 / 引擎身份与总执行耗时。

```json
{"schema_version":"gateway.progress.v1","stage":"generating","completed":5,"total":20,"message":"sampling"}
```

进度可选；声明 v1 的引擎写入 stage.json 时须遵守该格式，执行完成前会校验。失败用非零退出码和日志说明原因；网关映射为统一任务错误（code/message）。取消仍由网关终止整个进程组，脚本不能脱离进程组启动后台任务。

旧内置引擎由执行桥转换输入 / 结果；`contract: legacy` 的已有脚本仍可运行。它们经过同一个 task.json 边界，但内部仍可读取旧 input.json。HTTP 查询会把旧结果也规范化为 gateway.result.v1；没有重写所有底层引擎 CLI。

## 查询与编辑重试

| API | 含义 |
|---|---|
| GET `/api/v1/schema` | 请求、落盘任务、结果、进度 JSON Schema |
| POST `/api/v1/tasks/validate` | 只校验，不入队 |
| GET `/api/v1/tasks/{id}` | 状态、时间、完整输入、峰值、结果、快照、调用来源 |
| GET `/api/v1/tasks/{id}/assets/{asset_id}` | 预览 / 下载受控输入素材 |
| GET `/api/v1/tasks/{id}/artifacts/{index}` | 下载指定结果 |
| GET `/api/v1/tasks/{id}/draft` | 生成可编辑的新提交请求，包含全部参数和素材引用 |
| POST `/api/v1/tasks/{id}/retry` | 按旧执行快照原样重试，限失败 / 取消 / 中断 |
| POST `/api/v1/tasks/{id}/cancel` | 取消 |

详情页的“重试 / 编辑后提交”走 draft → 编辑 → POST /api/v1/tasks；允许从任何任务创建新提交，记录 derived_from，并使用提交时选中的当前配置。它与 API 的原样 retry 不同：后者保留旧执行快照。两种方式都不会覆盖原任务。

draft 使用已保存素材哈希，提交时会检测源文件是否被外部改动。新任务复制素材到自己的目录。修改参考图时可以移除旧素材、上传新素材；切换引擎不会自动移除不兼容输入，而会明确报错。

查询的 metrics 区分排队、执行、总耗时；RSS 是 1 秒采样的进程树 RSS 总和，不等于独占 GPU / Metal 内存。MLX allocation peak 是另一种统计。未记录的历史数据返回 null / 缺失，不能推算为真实峰值。

## Schema 与兼容

仓库 `schemas/*.schema.json` 由 model_gateway.task_v1 的 Pydantic 模型生成，与 `/api/v1/schema` 同源。旧 `/api/jobs`、火宝 OpenAI 图片 / MiniMax 视频路径保留；不会要求调用方同步升级。

当前未提供通用远端 HTTP 引擎适配器，也不实现 OpenAI 完整协议。v1 的统一是网关任务与引擎边界统一，不代表外部供应商请求格式相同。

队列操作新增 pause/resume/bump；`paused` 是非终态。任务响应提供 controls 与暂停计时，详见 [队列控制](QUEUE_CONTROL.md)。
