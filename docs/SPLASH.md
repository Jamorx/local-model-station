# Splash 直连与管理方案

本机发现 Homebrew 安装的 Splash 1.5.0。现有 Splash 进程由 oMLX 通过 watchdog 启动，使用动态回环端口，本次探测 `/v1/models` 返回鉴权错误。该进程的端口和生命周期归 oMLX 管理。

## 8777 只读视图

打开左侧「Splash 管理」。该页可见时每五秒查询 `GET /api/v1/engines/splash`，鉴权沿用网关 API。配置区从 `MEDIA_LAB_ROOT/omlx-data/model_settings.json` 读取 Splash 模型 ID 和允许展示的字段。运行区探测本机 Splash 1.5.0 HTTP 父进程的实际监听端口，并查询 `/ready`。当前支持 IPv4 本机监听，可发现 oMLX 分配的动态端口，也支持符合相同启动结构的独立实例。

就绪状态来自引擎探测，模型 ID 和资源设置来自启动参数，oMLX TTL 来自保存的模型配置。三者可能不同。查询不发送推理、不读取上游 Bearer 密钥。网关侧无需配置动态端口。

### 用量与日志数据源

- oMLX 用量表只读聚合 `usage.sqlite3` 的 `model_usage_hourly`。通过当前 Splash 配置和既有 Splash 日志文件名识别模型。统计按模型归属，同一模型更换运行时前的记录仍会计入。读取 SQLite 已落盘的快照，最新 WAL 写入可能稍后可见。
- Splash 请求日志读取最近更新的四个 oMLX 托管 Splash 日志，每个最多读取 32 KiB，并按日志文件更新时间、文件内倒序取最多 25 条。接口只返回完成及取消事件的时间、模型、提示、输出、缓存 token 与速度。日志时刻沿用源文件，没有日期。
- 独立 Splash 可通过原生 `/metrics` 显示该进程的完成请求数、预填充、输出、缓存复用 token、速度与引擎内存。重启后计数由 Splash 重新开始。
- 独立实例使用 LaunchAgent 时，让其 stdout/stderr 写入自有日志。给 8777 设置 `MEDIA_SPLASH_LOG` 为该请求日志的绝对路径，`MEDIA_SPLASH_PORT` 为实例端口。若该端口开启鉴权，再设置网关环境变量 `MEDIA_SPLASH_API_KEY`。网关只将这个密钥发送给该端口的回环指标请求。
- 独立使用时可继续查看当前进程统计以及日志里的近期请求。跨重启的累计历史需要另行聚合。oMLX 旧历史是可选的历史参考。

## 建立独立实例

使用独立 LaunchAgent 执行已安装的 `splash serve`，绑定固定回环端口（例如 8001，启动前确认空闲）。以下变量由运维环境提供，路径指向已准备、与 Splash 版本匹配的本地权重和 draft。

```sh
splash serve --host 127.0.0.1 --port 8001 --offline \
  --model "$SPLASH_MODEL_ID" --model-dir "$SPLASH_MODEL_DIR" \
  --draft-model "$SPLASH_DRAFT_DIR" --no-webui
```

实例就绪后查询其 `/v1/models`，以返回的 ID 配置调用方。与服务同机的 OpenAI 兼容客户端可直接使用 `http://127.0.0.1:8001/v1`。使用 8777 统一入口时，给网关设置：

```sh
export MEDIA_TEXT_BASE_URL=http://127.0.0.1:8001/v1
export MEDIA_TEXT_MODEL="$SPLASH_SERVED_MODEL_ID"
```

配置上游认证时同时设置 `MEDIA_TEXT_API_KEY`。显式指定文本上游后，网关只使用显式上游密钥。现有网关文本兼容层支持单模型 Chat Completions 及 SSE。Responses、Anthropic Messages 等接口可由支持的客户端直接请求 Splash。参考 [Splash 官方项目](https://github.com/incoai/splash)。

## 8777 管理入口设计

1. 先做只读状态，显示 LaunchAgent、固定回环端点、模型 ID、就绪状态和日志位置。模型列表来自实例 `/v1/models`。
2. 由管理员在本机维护可选 profile，固定模型路径、draft、上下文、内存预算和端口。管理 API 只接受配置中的 profile ID，使用固定参数数组管理独立 LaunchAgent。
3. 切换模型时停止接收新文本请求，等待现有流结束，再重启 Splash 并探测就绪。失败时恢复上一个可用 profile。分别验证断流、鉴权、并发和跨引擎内存占用。
4. 单独用已知兼容的 Qwen 权重验证普通与 SSE 请求，再迁移文本客户端。本机当前实例的实际表现不能外推到其他 GGUF 或 MLX 模型。

当前已有只读视图。独立 Splash 实例、生命周期管理界面、多模型动态路由及直连推理验收待实施。
