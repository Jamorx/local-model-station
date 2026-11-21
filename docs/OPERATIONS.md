# macOS 运维

首期仅支持 macOS / Apple Silicon 推理环境。默认端口 8777，配置 / 示例 / 脚本均从源码目录使用，暂不支持脱离源码的 wheel 安装。

## 环境

Python 3.12 API 环境安装 `requirements-api.txt`；MLX、torch、diffusers、引擎二进制按各引擎安装到自己的环境。`MEDIA_RUNNER_PYTHON` 默认 `~/LocalModelLab/venv/bin/python`。内置 MOSS Edit、H3 Image 使用独立 venv，参考 `builtin_runner.py`。FFmpeg 默认 `/opt/homebrew/bin`，其他路径通过环境变量覆盖。

`.env.example` 是参考，不会自动加载。变量需要 shell export，或由服务管理器提供。模型不会在提交时自动下载。网关设置 Hugging Face 离线标记，但不阻止自定义脚本主动联网。

## LaunchAgent

在源码根目录，使用 API 环境的 Python：

```sh
MEDIA_EXECUTION_ENABLED=1 .venv/bin/python scripts/service.py start
.venv/bin/python scripts/service.py status
.venv/bin/python scripts/service.py stop
```

后台服务标签 `local.model-gateway`，监听 `0.0.0.0:8777`。默认数据根目录 `~/Library/Application Support/LocalModelGateway`，包含 jobs、stdout.log、stderr.log；可用 `MEDIA_DATA_ROOT` 覆盖。LaunchAgent 是当前用户登录会话服务，不是开机前系统服务。

启动默认暂停执行；明确 `MEDIA_EXECUTION_ENABLED=1` 才推理。服务脚本保存 MEDIA_* 环境到权限 0600 的 plist；不要提交 plist。正在生成时 stop 拒绝执行，避免中断用户任务。启动发现端口占用会拒绝，不杀已有服务。

配置更改不会热生效；等任务空闲后 stop/start。退出进程会让运行中的任务中断，重启后需对账再人工重试。不要用多个 uvicorn workers；同任务目录进程锁会拒绝重复实例。

## 从旧 video-production-system 迁移

1. 独立代码先使用 18777 和独立任务目录验收，不与 8777 共用执行目录。
2. 暂停调用方的新提交，建立维护窗口（旧服务没有原子 drain 接口）。等 8777 的 active_job 为空，检查无 queued/running 任务，再停用旧 LaunchAgent。正在处理的任务不强制中止。
3. 备份旧 plist。复制旧 jobs 到独立数据根目录，逐文件 SHA-256 校验；原目录保留作回滚备份。
4. 用新标签启动 8777，检查 service_id、任务历史、图片 / 视频下载、火宝配置与图片生成回归。
5. 成功后旧 LaunchAgent 保持卸载；旧源码与任务目录保留，8766 不受影响。公开仓库只含源码，不含备份、日志、生成媒体、密钥。

可使用 `python scripts/migrate_legacy.py /absolute/path/to/video-production-system` 执行空闲检查、停旧服务、复制校验、启动新服务。它会拒绝忙碌队列、已有目标 jobs 目录和错误服务身份；备份写在被 Git 忽略的 runtime-data 下。

迁移不修改原任务 ID 和产物内容。历史已完成任务保留原参数，首次迁移尚无执行快照；历史排队任务启动时根据新注册表创建快照。为避免配置含义变化，应空队列迁移。历史失败任务重试时若无快照，采用当时可用的注册表；不要将此解释为完全复现旧引擎版本。

回滚前确保新服务无活跃任务；卸载新服务，恢复旧 plist 并 bootstrap。迁移后产生的新任务需额外对账，不能直接用旧快照覆盖新数据。

## 资源与故障

默认最多 20 个未完成任务、单模型执行；每个 profile 声明可用内存门槛。内存等待有超时；可用内存持续低于 2 GiB 或任务期间 swap 增长超过 8 GiB 会终止进程组。该策略是整机启发式保护，不是精确 Metal 显存归属。

ETA 来自历史相似任务，不能作为完成承诺。优先看 stage / run.log。排队但不运行时依次检查 execution_enabled、可用内存、probe 缺失项、其它推理测试进程、日志。

当前任务存储是本地 JSON + 原子 rename + 单进程锁。尚无 fsync 级断电事务保证、多机调度或分布式幂等；不要用网络共享目录承载队列。

## Production cutover: 2026-11-14

At the user's explicit request, the old active inference was stopped and the standalone `local.model-gateway` LaunchAgent took over 0.0.0.0:8777. Preview port 18777 is closed. Service 8766 was not modified.

31 historical jobs were copied to `~/Library/Application Support/LocalModelGateway/jobs`; all 188 copied files matched SHA-256 before migration updates. Five queued jobs are now paused with execution snapshots from the current registry. One formerly running job is interrupted and requires a new retry, not checkpoint resume. New submissions can run; old paused jobs require manual resume.

The old jobs remain intact. The old LaunchAgent is unloaded and its plist removed from the login autoload directory, with a backup retained at `runtime-data/cutover-20261114-223324`. That directory contains the original plist, before-status records, copied-file hashes and migration result. Do not publish these runtime files.

Verified LAN health, page, job list, queue, v1 schema and paused task detail. No fresh model generation was submitted. The generic migration script still refuses busy queues; this was a separately authorized maintenance cutover.
