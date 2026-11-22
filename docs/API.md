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

## 服务端批次回调

由 8777 等待任务，并把结果发送到原 DSH Web 或本机 Codex 会话。提交方不需要安装等待器。部署者在任务目录的父目录创建 `dsh-web.json`，默认位于 `~/Library/Application Support/LocalModelGateway/dsh-web.json`。

```json
{"url":"http://dsh-host.lan:3443","result_base_url":"http://mac-host.lan:8777"}
```

只配置可信的 DSH Web 地址。配置文件由部署者管理。若该 DSH Web 需要 Cookie，可通过 8777 进程环境变量 `DSH_WEB_COOKIE` 提供。8777 的回调登记与查询默认只允许回环来源。如果提交设备在局域网另一台机器上，部署者设置 `MEDIA_DSH_CALLBACK_KEY`，客户端附上 `X-DSH-Callback-Key`。设置了 `MEDIA_API_KEY` 时仍需常规 Bearer 鉴权。

1. 通过 `POST /api/v1/tasks` 提交任务并记录任务 ID。
2. 调用 `POST /api/v1/callbacks`，传入 `{"target":"dsh-web","task_ids":["<任务 ID>"],"session_id":"<当前 DSH_SESSION_ID>"}`。同批任务与同一会话重复登记会返回同一通知 ID。
3. 收到 202 后即可结束当前轮次。任务全部到达终态后，8777 将状态、错误摘要和产物链接排入 DSH Web 原会话。
4. `GET /api/v1/callbacks/{id}` 查询登记状态。`delivered` 对 DSH Web 表示消息已被接收，对 Codex 表示 CLI 成功完成续跑。若投递超时、收到错误或服务在投递中重启，显示 `uncertain`，先核对原聊天，服务不会自动再开一轮。

DSH 用户可以直接说「提交任务，完成后唤醒这条会话验收」。代理从 DSH 提供的环境变量读取当前 `DSH_SESSION_ID`。8777 的回调运行在自己的后台线程，等待时不调用文本模型。

### Codex 服务端投递

同一个 `POST /api/v1/callbacks` 支持 `target: codex` 和 Codex 会话 UUID。部署者在同一目录创建 `codex-callback.json`，例如：

```json
{"cwd":"/absolute/path/to/workspace","executable":"/opt/homebrew/bin/codex","result_base_url":"http://127.0.0.1:8777"}
```

可选 `model` 覆盖 Codex CLI 的默认模型。会话必须存在于 8777 运行账户可访问的 Codex 存储中，模型和登录配置也必须可用。服务端以只读沙箱、非交互审批执行，5 分钟超时后标为 `uncertain`。DSH Web 和 Codex 的投递独立运行。

## DSH 和 Codex 批次完成通知

提交端保存本批任务的 ID，并在能访问 8777 的设备上运行等待器。等待器只查询这些任务，每批一次性返回成功、失败、取消或中断状态、产物链接和错误摘要。等待期间无需运行模型。8777 使用 Bearer 鉴权时，在客户端环境中设置 `MEDIA_API_KEY`。

### 在提交设备安装

有 Python 3 的设备可从 8777 的 `GET /api/v1/clients/batch_wait.py` 下载客户端，也可使用仓库中的 [batch_wait.py](../model_gateway/batch_wait.py)。部署地址替换为执行端 Mac 的实际局域网地址。该文件仅用 Python 标准库。请从信任的仓库版本下载。已安装本项目的设备也可用 `lmg-batch` 或 `lmg wait-batch`。等待可前台运行，后台启动和恢复会话支持 macOS／Linux。

```sh
curl -fSLo batch_wait.py http://mac-host.lan:8777/api/v1/clients/batch_wait.py
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

### DSH Web 日常会话

对于由 DSH Web 托管的聊天，使用 `--resume-agent dsh-web`。该模式用 Web 的 `session/prompt` 排队，由原 Web 进程启动验收轮次。会话保持打开即可。CLI 的 `dsh` 模式适用于 headless 会话。

```sh
python3 batch_wait.py --url http://mac-host.lan:8777 "$TASK_ID" \
  --detach --log "$LOG_PATH" --receipt "$RECEIPT_PATH" \
  --resume-agent dsh-web --dsh-web-url "$DSH_WEB_URL"
```

未显式指定 `--session-id` 时，等待器从 `DSH_SESSION_ID` 读取当前会话。DSH Web 使用自身的访问规则。需要浏览器凭据的部署可由操作者显式提供 `DSH_WEB_COOKIE`，该值只发送给所配置的 DSH Web 服务。Web 回执确认消息已入队，最终验收结果请查看原聊天。

可将 [Local Model Station 技能](../skills/local-model-station/SKILL.md) 安装到 DSH 的技能目录，使其能从简短的提交和验收指令执行完整流程。
