# 调用协议

推荐使用 [Gateway Task v1](GATEWAY_TASK_V1.md)。以下旧 `/api/jobs` 接口保留兼容，也会生成统一 task.json。

首期仅支持 macOS。以运行服务的 `/openapi.json` 为字段 Schema；本文描述生命周期、语义和兼容边界。API 尚处于 0.x，协议变更需写入 CHANGELOG。

## 原生异步协议

| 方法 / 路径 | 用途 |
|---|---|
| GET `/health` | 内存、swap、执行开关、活跃任务 |
| GET `/api/engines` | 引擎类型与绑定的 profiles |
| GET `/api/models` | profile 列表、模型身份、引擎、内存估算与依赖状态 |
| GET `/api/models/{profile}/probe` | 静态依赖检查；不加载权重 |
| POST `/api/validate` | 校验请求并预览执行配置，不入队、不推理 |
| POST `/api/jobs` | 入队，HTTP 202 |
| GET `/api/jobs` | 全部任务（含历史记录），按创建时间倒序；支持 ETag |
| GET `/api/jobs/{id}` | 状态、参数、元数据、执行快照 |
| POST `/api/jobs/{id}/cancel` | 排队任务立即取消；运行任务请求终止进程组 |
| POST `/api/jobs/{id}/retry` | 失败、取消、中断任务建立新 ID，复制输入与执行快照 |
| POST `/api/jobs/{id}/bump` | 提升排队优先级 |
| GET `/api/jobs/{id}/log` | 日志末尾 16 KB |
| GET `/api/jobs/{id}/result` | 结果清单与实际耗时 |
| GET `/api/jobs/{id}/output` | 下载实际产物 |

图片 / 视频原有调用继续使用 `model` 字段，其值是 profile ID。`kind` 为 `image`、`video` 或 `audio`。通用字段包含 `prompt`、`seed`、`width`、`height`；`options` 为注册表声明的引擎扩展参数。未知顶层字段和未知 options 被拒绝。

`metadata` 是最多 32 项字符串映射，可携带 client/project/episode/shot；网关不管理这些业务对象，也不验证业务版本。

```json
{"kind":"audio","model":"qwen3-tts","prompt":"你好","options":{"temperature":0.7},"metadata":{"client":"my-agent"}}
```

内置图像 / 视频模型保留首尾帧、参考图、参考音频、参考视频、LoRA 等字段及各自限制。以 `/docs` 和 `docs/COMPATIBILITY.md` 为准。脚本引擎通过 profile.input_roles 声明媒体角色，使用 Gateway Task v1 的统一输入文件契约。

## 状态与重试

`queued ↔ paused`、`queued → running → succeeded | failed | cancelled`；已验证的引擎另支持 `running ↔ paused`；运行时服务重启将遗留 `running` 标记为 `interrupted`，不自动重复推理。排队任务按时间和插队标记排序，内存不足时可让资源需求较小的后续任务先执行。

HTTP 202 只代表接收，不代表模型成功运行。内存不足会等待，等待或执行超时会失败。默认单任务并发、最多 20 个未完成任务。提交接口不保证实时启动。

提交和重试可带 `Idempotency-Key`：同键同规范化请求返回原任务；同键不同请求返回 409。键随任务记录持久化，删除任务记录后不再保证该键去重。配置变化可能改变默认参数，跨配置重试建议显式提供参数。客户端必须保存返回的任务 ID；火宝是否重发幂等键由其自身实现决定。

`retry` 复用原配置与种子。希望换引擎、换种子或新配置时，应创建新的提交。

| 状态码 | 含义 |
|---|---|
| 401 | 密钥无效 |
| 404 | 任务 / 模型 / 路径不存在 |
| 409 | 幂等冲突、依赖缺失、状态不允许 |
| 422 | 字段、模型能力或附件不符合约束 |
| 429 | 未完成任务达到上限 |

错误 JSON 使用 FastAPI `detail`，可能为字符串、结构对象或字段错误数组。提交失败不应直接无限重试。

## 访问与边界

设置 `MEDIA_API_KEY` 后发送 `Authorization: Bearer ...`。未设置时允许可信私网 / 回环来源访问；这不是多用户鉴权。健康页与静态 UI 公开，任务数据 API 使用鉴权。默认 HTTP，仅用于本机或可信局域网。

产物必须是任务目录内非空文件，拒绝绝对路径、越界与越界符号链接。原生下载保留真实扩展名。兼容接口按产物实际扩展名返回下载 URL；调用方是否支持额外格式需要单独验收。未知兼容请求参数会拒绝，避免静默丢弃。

暂停、恢复、排序与批量操作详见 [队列控制](QUEUE_CONTROL.md)。

## DSH 和 Codex 批次完成通知

提交端保存本批任务的 ID，并在能访问 8777 的设备上运行等待器。等待器只查询这些任务，每批一次性返回成功、失败、取消或中断状态、产物链接和错误摘要。等待期间无需运行模型。8777 使用 Bearer 鉴权时，在客户端环境中设置 `MEDIA_API_KEY`。

### 在提交设备安装

有 Python 3 的设备可以把 [batch_wait.py](../model_gateway/batch_wait.py) 保存到本机，直接运行。该文件仅用 Python 标准库。请从信任的仓库版本下载。已安装本项目的设备也可用 `lmg-batch` 或 `lmg wait-batch`。等待可前台运行，后台启动和恢复会话支持 macOS／Linux。

```sh
python3 batch_wait.py --url http://mac-host.lan:8777 "$TASK_ID_1" "$TASK_ID_2"
```

### 结束当前轮次后恢复原会话

在提交轮启动下列命令，拿到 PID 后结束本轮。等待进程会在整批结束后尝试唤醒原会话。

```sh
python3 batch_wait.py --url http://mac-host.lan:8777 "$TASK_ID_1" "$TASK_ID_2" \
  --detach --log "$HOME/.local/state/lmg/review.log" \
  --resume-agent dsh --session-id "$SESSION_ID" --session-cwd "$WORKSPACE" \
  --receipt "$HOME/.local/state/lmg/review-receipt.json"
```

Codex 使用 `--resume-agent codex`，可选 `--resume-model` 指定账户支持的模型。启动进程必须能够在 `PATH` 找到相应 CLI，访问其账户、会话存储和原工作目录。DSH 与 Codex 的会话 ID 属于各自的会话存储。局域网设备可以各自运行客户端；仅复制 ID 到另一台设备无法迁移会话。应在**能打开原会话的设备**启动等待器。跨设备会话迁移依赖各客户端自身能力，本项目只传递任务结果。

DSH 恢复时使用目标 headless profile 的本机权限配置。同一个会话若仍被桌面窗口或其他进程持有写入锁，DSH 可能拒绝恢复。先结束当前轮次；若锁仍在，释放会话占用后核对日志。Codex 恢复使用只读沙箱和非交互审批，适合检查结果。若验收要求修改文件或写入外部系统，应在常规会话中按原授权执行。后台进程以当前 OS 用户身份运行，无法替其他账户恢复会话。

每批使用独立的日志和 receipt 路径。重跑时沿用原 receipt，已投递批次会跳过重复续跑。日志可能含产物链接或模型回复，请留在本机。若进程在投递途中退出或 CLI 返回错误，先核对会话和日志；`sending` 表示送达待确认。后台进程可跨提交命令退出运行，机器重启或执行环境主动清理进程后，可用原参数和 receipt 重新启动。
