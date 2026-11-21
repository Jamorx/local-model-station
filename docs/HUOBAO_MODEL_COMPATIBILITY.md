# 火宝模型配置兼容（2026-11-14）

## 实际配置盘点

只读查询本机火宝 `ai_service_configs`，不读取输出 API key。本机启用三条配置：

| 类型 | provider | 模型 | 当前 Base URL | 8777 对应能力 |
|---|---|---|---|---|
| text | openai | Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S | 8000/v1 | `/v1/chat/completions`，非流式和 SSE 透传到本地 oMLX |
| image | openai | zimage | 5881 | `/v1/images/generations`、multipart `/v1/images/edits`、`/v1/images/task/{id}` |
| video | minimax | MiniMax-H3 | 5880 | `/v2/video_generation`、`/v2/query/video_generation/{id}` |

检查的是当前部署配置及火宝源码中的真实适配器，不是火宝设置页列出的所有云供应商。Gemini、Volcengine、Aliyun 等云模型未配置相应本地引擎，不由 8777 冒充；仍可由火宝直连原服务。

## 已补齐

- 火宝图片、视频适配器可直接提交至 8777，取得异步任务 ID 并按原协议轮询，输出 URL 指向 8777。无需再经 5880/5881 才能进入队列。
- 图片模型支持 zimage、moss、flux3、h3-image、moss-edit（目录必须存在）。参考编辑仅支持 zimage / moss-edit / h3-image，映射到正确参考字段。
- 画布按本地限制等比缩小并向下 32 对齐，例如 1080×1920 → 864×1536，提交响应返回实际 size。不会静默改成另一个模型。
- H3 支持文本、首帧、尾帧、有序参考图、参考视频及多段参考音频进入同一队列；重试复制所有参考文件。参考音频经 FFmpeg 转为 32kHz 单声道 WAV。
- 时长支持 1–15 整数秒输入，火宝适配器正常使用 4–15 秒；runner 保留请求帧数，实际输出时长以 ffprobe 结果为准。
- 提示词上限扩到 7000 字符，不再继承旧 shim 静默截断中段的行为。
- 新媒体接口支持 Idempotency-Key；火宝当前适配器默认不发送此头，所以尚不能宣称火宝端具备完整提交去重。
- `/v1/models` 提供兼容模型目录；`/api/compatibility` 明确协议、模型和限制。

## 明确限制

1. H3 直接生视频仅支持 768P 档；2K 直生返回 422。现有 vpipe-2k 是“已有视频底片重制”，不等价于火宝的一个 2K 直生请求。
2. `vpipe-h3` 为经 vpipe 的 H3 生成通道（首帧/尾帧 + prompt，最长 10 秒，pika_flex_fl2va 6 步 / turbo_fl2va_lite 4 步 / 无 LoRA 16 步），产物自带同步生成音轨；与 h3（h3-bin 直跑）互为并行通道，参考图 Ref2VA 仍只走 h3。
2. 本地 H3 首帧与 reference_image 混用仍拒绝，避免接受引擎互斥组合。
3. 图片编辑当前只能接一张参考图；多图明确返回 422，不沿用旧 shim 只取第一张的做法。需要多角色一致性合成的镜头应选择确实支持多参考的模型后端。
4. 图片 n 仅支持 1；不会通过兼容层开启候选流程。
5. 素材支持 data URL，或 `MEDIA_HUOBAO_BASE_URL` 同源的 `/static/` 地址；不开放任意 URL 或 file:// 读取。火宝本地参考图通常已转换成 data URL。
6. 文本转发仍使用 oMLX 自身调度，未纳入重媒体 worker 互斥。8777 内存门槛仍保护图片／视频生成，但不是跨服务的全局内存预留。
7. 尾帧、参考视频、多音频已验证入队、重试复制及 runner 参数传递，未进行真实多模态推理验收。

## 切换方式（已于2026-11-14完成本机切换）

火宝 provider 和 model 保持原值，仅修改 Base URL：

- 图片 openai：`http://127.0.0.1:8777`
- 视频 minimax：`http://127.0.0.1:8777`
- 文本 openai：`http://127.0.0.1:8777/v1`；也可以继续直连 8000/v1，少一次转发。

火宝后台若运行在其他机器，改为服务器局域网地址。通过火宝配置界面／API 修改，不直接写数据库。后续用户授权后已通过 API 切换图片与视频配置，文本保留直连 8000；旧 shim 未改动，脱敏旧配置已备份。详见 [真实生成验收](HUOBAO_LIVE_ACCEPTANCE.md)。已有 5880 任务 ID 不能直接向 8777 轮询；先保留旧配置处理在途／历史恢复，或完成任务映射后再替换。

## 环境变量

`MEDIA_TEXT_BASE_URL`（默认 8000/v1）、`MEDIA_TEXT_MODEL`（默认当前 Flash 名称）、`MEDIA_TEXT_API_KEY`、`MEDIA_HUOBAO_BASE_URL`（默认本机 3013）、`MEDIA_PUBLIC_BASE_URL`（默认请求 origin）。文本上游密钥未设置时可读取现有本机 oMLX 配置，客户端凭据不会透传给上游。启动脚本支持传入这些变量。

## 验证

42 项隔离测试通过：请求校验、图片尺寸和模型拒绝、multipart 编辑、异步状态、去重、多模态文件与重试、实际 runner 参数构建、文本 JSON/SSE 转发等。

另使用本机火宝原始 OpenAIImageAdapter 和 MiniMaxVideoAdapter 对临时 8777 同构服务发出真实格式请求，并调用其响应解析器：文生图、图片编辑、H3 首帧视频及轮询均通过。该测试关闭执行，未加载图像／视频模型。

8777 实际调用本机 Flash：普通 JSON 和 SSE 流式请求均返回 200 且结构验证通过；FFmpeg 实测将 16kHz 测试 WAV 转为 32kHz 单声道，时长保留。图片／视频本轮不触发重模型生成。协议兼容不等同全部模型输出质量、吞吐及高级组合验收通过。
