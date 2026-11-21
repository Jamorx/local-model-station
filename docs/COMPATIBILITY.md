# 调用方兼容范围

| 调用方 / 协议 | 当前状态 | 范围 |
|---|---|---|
| 原生 `/api/jobs` | 支持 | image/video/audio、队列、取消、重试、日志、结果 |
| 火宝 OpenAI 风格图片 | 保留已有适配 | generations、单图 multipart edits、异步任务查询；n=1 |
| 火宝 MiniMax 视频 | 保留已有适配 | MiniMax-H3、768P、首尾帧及参考音视频限制按 H3 引擎 |
| OpenAI 文本 | 可选转发 | 已配置文本模型的 JSON / SSE；火宝可继续直连 8000 |
| OpenAI 标准同步生图 | 不声明完全兼容 | 本服务图片接口返回异步 task_id，不能当成通用同步图片 API |
| OpenAI `/v1/audio/speech` | 未实现 | 音频目前走原生 `/api/jobs` |
| Phosphene 外部统一后端 | 未接通 | 现有检查未发现可无损统一接入的配置；不伪装云服务 |
| 通用异步 HTTP 引擎 | 后续 | 需要上游 ID 持久化、取消、提交失败对账 |

火宝图片与视频 base URL 可保持 `http://127.0.0.1:8777`（火宝与网关同机），文本为 `http://127.0.0.1:8000/v1`。跨主机时用 Mac 的局域网地址，设置 `MEDIA_PUBLIC_BASE_URL` 确保产物 URL 对调用方可达。

图片接口：`POST /v1/images/generations`、`POST /v1/images/edits`、`GET /v1/images/task/{id}`。超出本地画布范围会按比例缩小并 32 对齐。兼容编辑仅一张输入图，未知模型拒绝，不静默替换。

视频接口：`POST /v2/video_generation`、`GET /v2/query/video_generation/{id}`。不支持 2K 直接生成；vpipe-2k 是另一个需要底片的原生 profile。首帧与 Ref2VA 参考图不能组合。请求时长与编码后实际时长可能不同，以 result.ffprobe 为准。

素材支持 data URL，以及配置的火宝源同 origin `/static/` 路径；不接受任意远程 URL、重定向或本地 file URL。音频输入会使用 FFmpeg 转换为引擎支持的 WAV。

详细旧版本协议和真实验收证据见 `HUOBAO_MODEL_COMPATIBILITY.md`、`HUOBAO_LIVE_ACCEPTANCE.md`。它们是提取前的历史记录，不代表新版本的所有引擎均再次实测；当前验证以 `VALIDATION.md` 为准。
