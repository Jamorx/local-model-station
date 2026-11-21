# 队列控制与暂停

首期 macOS。控制台、旧任务 API 与 Gateway Task v1 使用同一个调度器。

## 三种不同的暂停

| 操作 | 当前任务 | 后续任务 | 内存 |
|---|---|---|---|
| 暂停排队中的单个任务 | 尚未执行，保留全部输入 | 其他任务可运行 | 不加载该模型 |
| 暂停后续调度 | 正在运行的任务继续 | 全部暂不启动 | 当前任务继续占用 |
| 挂起运行中的任务 | 仅显式验证并启用的引擎支持 | 仍占用唯一执行槽 | 不释放模型内存 |

H3 / h3.c、vpipe、MLX 等现有内置引擎**默认不开放运行中挂起**，因为尚未完成相应验证。界面会禁用其运行中暂停按钮并说明原因。要让当前视频完成后不再启动下一项，可使用“暂停后续调度”；要停止当前推理并释放资源，应取消，之后编辑重试。

运行中挂起使用进程组暂停，不是模型检查点。恢复需要同一服务进程和同一推理进程仍然存在；不能把它当作跨重启续跑。正常停服会结束当前进程；异常退出后历史运行 / 挂起任务标记中断，需要人工对账，强杀服务遗留的进程不得按旧 PID 盲目恢复。

## 日常操作

- 单任务：暂停、恢复、置顶、上移、下移、移到队尾、取消、复制编辑 / 编辑重试。
- 批量：暂停、恢复、置顶、移到队尾、取消。界面仅提交适用项；失败和不支持项保留选中，并逐项说明结果。
- 置顶不抢占正在执行的任务。内存不足时，后续较小任务仍可能先执行；队列顺序不是严格资源预留。
- 恢复后续调度不会恢复单独暂停的任务，也不会绕过启动配置 `MEDIA_EXECUTION_ENABLED=0`。
- 按执行顺序查看时，运行 / 运行中挂起任务优先，其后是排队、单独暂停、历史记录。
- 全部未完成任务都会返回列表，不会因为历史记录超过 100 条而隐藏老的暂停任务。另返回最近 100 条结束记录。

## API

| 方法 / 路径 | 功能 |
|---|---|
| GET `/api/queue` | 调度开关、未完成数量、实际排队顺序 |
| POST `/api/queue/control` | `{"action":"pause"}` 或 `{"action":"resume"}` |
| POST `/api/jobs/{id}/pause` | 暂停单个任务；不支持运行中挂起的引擎返回 409 |
| POST `/api/jobs/{id}/resume` | 恢复单个任务 |
| POST `/api/jobs/{id}/bump` | 放到当前队首 |
| POST `/api/jobs/{id}/up`、`down` | 上移 / 下移一个位置 |
| POST `/api/jobs/{id}/defer` | 放到当前队尾 |
| POST `/api/queue/batch` | `{"action":"pause","ids":["任务ID"]}` |

Gateway Task v1 也提供 `/api/v1/tasks/{id}/pause`、`resume`、`bump`。队列控制接口是各协议共用的管理接口。

批量支持 pause/resume/cancel/bump/defer，最多 100 个 ID，重复 ID 去重。响应 `results` 为逐项 ok / error / status_code，不是全部成功的承诺。批量置顶保留输入 ID 的相对顺序。

任务查询新增 `controls` 表明当前可用操作，以及 `pause_requested`、`cancel_requested`、`paused_from`。运行中挂起与恢复由 worker 执行，收到请求到真正切换状态可能有短暂延迟；界面显示“暂停中 / 恢复中 / 取消中”。

## 状态、计时与持久化

- `queued → paused → queued`：单独暂停等待中的任务，重启仍保持暂停。
- `running → paused → running`：仅支持挂起的引擎；paused_from=running，仍占用执行槽。
- 取消运行中挂起任务会先恢复进程组，再终止，避免留下不能处理退出信号的挂起进程。
- 暂停中的任务也计入最多 20 个未完成任务，不能用暂停绕过容量限制。
- 手动暂停和暂停后续调度期间，不增加排队超时计时。运行中挂起时间不计入执行超时；内存保护仍有效。
- 详情区分执行经过时间、有效执行时间、运行中暂停时长。总耗时仍是创建到结束的墙钟时间。
- 队列暂停开关保存在任务根目录 queue-state.json；顺序和单任务暂停状态保存在 job.json。与既有任务文件一样，使用原子替换和单进程锁，不承诺跨文件断电事务。

## 引擎接入

引擎默认 `supports_process_pause=false`。只有在隔离环境确认进程组挂起、恢复、取消、超时和资源占用行为后，才可设置 true。配置被冻结到执行快照，修改注册表不会改变已提交任务的挂起能力。

本轮仅使用 CPU 心跳测试引擎验证实际挂起 / 恢复 / 取消，不代表 GPU 引擎已验证。暂停进程不会让出显存或执行槽，也不能用于抢占式并发调度。

## CLI

```sh
python -m model_gateway queue
python -m model_gateway queue-pause
python -m model_gateway queue-resume
python -m model_gateway pause JOB_ID
python -m model_gateway resume JOB_ID
python -m model_gateway bump JOB_ID
python -m model_gateway up JOB_ID
python -m model_gateway down JOB_ID
python -m model_gateway defer JOB_ID
```

隔离预览时在子命令前加 `--url http://127.0.0.1:18777`。CLI 也遵守运行中挂起能力限制。
