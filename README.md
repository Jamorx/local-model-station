# Local Model Station

面向 Apple Silicon 的本地模型工作台。当前公开版本是只读监控预览，提供 Splash 实例状态、原生指标、近期请求用量，以及模型任务协议。

## 快速开始

需要 macOS 和 Python 3.12+。Splash 由你自行安装和启动。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
local-model-station
```

打开 [API 浏览器](http://127.0.0.1:8777/docs)。与已有工作台并行运行时可指定其他端口，例如 `local-model-station --port 18777`。

| 入口 | 用途 |
|---|---|
| `GET /health` | 检查监控服务 |
| `GET /api/v1/engines/splash` | 获取本机实例就绪状态、原生指标和可读取的历史 |
| `POST /api/v1/tasks/validate` | 检查请求是否符合 Gateway Task v1 结构 |

服务默认只绑定回环地址。状态与日志取自本机，可用环境变量包括：

- `MEDIA_SPLASH_PORT`，用于给独立实例的请求日志匹配模型，以及限定可接收指标密钥的端口。
- `MEDIA_SPLASH_API_KEY`，可选的 Splash 指标端点 Bearer 密钥。
- `MEDIA_SPLASH_LOG`，独立 Splash 请求日志的绝对路径。
- `MEDIA_LAB_ROOT`，可选的既有运行目录，默认 `~/LocalModelLab`。如果目录里有 oMLX 统计和日志，监控可只读展示。

Splash 可独立运行。监控采样通过引擎的只读端点，推理客户端直接连接该引擎。

## 发布范围

首发含监控 API、OpenAPI 浏览器、Pydantic 请求模型及 [JSON Schema](schemas/) 。媒体任务执行、下载工作器、生产控制台及继承的引擎适配器尚未纳入本公开版。任务校验检查协议结构，执行能力和模型权重由具体引擎提供。

项目采用 [Apache License 2.0](LICENSE)。引擎、第三方 Python 依赖和模型权重适用各自的许可证。
