# aistudio-api (Enhanced Fork)

<p align="center">
  <a href="https://github.com/DeconstructedCube/aistudio-api"><img src="https://img.shields.io/badge/上游-DeconstructedCube-blue?style=flat-square&logo=github" alt="Upstream"></a>
  <a href="https://github.com/qishiwan16-hub/aistudio-api"><img src="https://img.shields.io/badge/Fork-qishiwan16--hub-orange?style=flat-square&logo=github" alt="Fork"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/Framework-FastAPI-009688?style=flat-square&logo=fastapi" alt="FastAPI"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="License"></a>
</p>

<p align="center">
  Google AI Studio 反向代理 → 标准 API 接口，增强版 Fork。<br>
  在上游基础上增加了 <b>OpenAI 兼容接口、假流式模式、抗截断、心跳保活、日志监控面板</b> 等实用功能。
</p>

---

## 目录

- [与上游的区别](#与上游的区别)
- [快速开始](#快速开始)
- [特色功能详解](#特色功能详解)
- [日志监控面板](#日志监控面板)
- [稳定性优化](#稳定性优化)
- [配置与环境变量](#配置与环境变量)
- [API 调用示例](#api-调用示例)
- [常见问题](#常见问题)
- [开源协议](#开源协议)

---

## 与上游的区别

本 Fork 基于 [DeconstructedCube/aistudio-api](https://github.com/DeconstructedCube/aistudio-api)，定期同步上游更新，额外增加了以下功能：

| 功能 | 上游 | 本 Fork |
|---|---|---|
| Gemini 原生接口 (`/v1beta/`) | ✅ | ✅ |
| OpenAI 兼容接口 (`/v1/chat/completions`) | ❌ | ✅ |
| 假流式模式（后台收完再返回 + 心跳保活） | ❌ | ✅ |
| 抗截断模式（合成工具调用绕输出长度限制） | ❌ | ✅ |
| 心跳保活（防止中转站/前端超时断连） | ❌ | ✅ |
| 日志监控面板（请求状态/token 统计可视化） | ❌ | ✅ |
| BotGuard 握手容错增强（重试/刷新/模板校验） | 基础 | 大幅增强 |
| 切号速度优化（跳过冗余步骤/GC 优化） | 基础 | 优化 |
| V8 堆内存 | 128MB | 512MB |

---

## 快速开始

### Android (Termux)

> 请从 [F-Droid](https://f-droid.org/packages/com.termux/) 或 [GitHub Releases](https://github.com/termux/termux-app/releases) 安装 Termux，**不要用 Play 商店版**。

```bash
# 1. 更新环境
pkg update -y && pkg install -y git

# 2. 克隆本 Fork
git clone https://github.com/qishiwan16-hub/aistudio-api.git
cd aistudio-api

# 3. 安装依赖
bash scripts/setup-env.sh
uv sync

# 4. 安装浏览器（仅首次）
bash scripts/setup-browser.sh

# 5. 启动服务
uv run python main.py server --port 8080
```

启动后访问 `http://localhost:8080` 进入管理面板，在「账号管理」粘贴 Google Cookie 即可使用。

> **后台保活**：下拉通知栏点 Termux 的 `Acquire Wakelock`，并在系统设置中为 Termux 关闭电池优化。

### Windows / Linux / macOS

```bash
# 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh  # Linux/macOS
# Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

git clone https://github.com/qishiwan16-hub/aistudio-api.git
cd aistudio-api
uv sync
bash scripts/setup-browser.sh  # Windows: scripts\install-browser.bat
uv run python main.py server --port 8080
```

### Docker

```bash
docker run -d \
  --name aistudio-api \
  --restart unless-stopped \
  -p 8080:8080 \
  -v aistudio-api-data:/app/data \
  ghcr.io/deconstructedcube/aistudio-api:latest
```

---

## 特色功能详解

### 🔌 OpenAI 兼容接口

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat Completions 格式，支持流式/非流式 |
| `GET /v1/models` | OpenAI 格式模型列表（含假流式/抗截断变体） |

可以直接对接 **SillyTavern（酒馆）**、**New API**、**One API** 等 OpenAI 兼容前端，无需额外转换。

**典型部署链路**：
```
酒馆/ChatBox → New API (:3000) → aistudio-api (:8080) /v1/chat/completions → Gemini
```

### 🎭 假流式模式

在模型名前加 `假流式-` 前缀触发（如 `假流式-gemini-4-argon`）。

- 后台用**真流式接收数据**，完整收到后一次性返回给客户端
- 接收期间**持续发送心跳**，防止前端/中转站判定超时断开
- 适合 New API 等需要完整响应才能做 token 统计的中转站

### 🛡️ 抗截断模式

在模型名前加 `抗截断-` 前缀触发（如 `抗截断-gemini-4-argon`）。

- 注入合成工具调用指令，让模型在输出被截断时通过 function call 继续生成
- 自动检测截断并拼接后续内容
- 工具名随机生成，避免模型记忆
- 可与假流式叠加：`假流式-抗截断-gemini-4-argon`

### 💓 心跳保活

- 所有流式请求从 BotGuard Hook 拦截成功后即刻开始发送心跳
- 心跳为空内容 SSE 事件，能穿透 New API 等中转站
- 彻底解决长时间思考/生成时前端显示「连接已断开」的问题

---

## 日志监控面板

内置轻量日志查看服务（端口 18082），提供两个页面：

| 页面 | 地址 | 功能 |
|---|---|---|
| 请求监控 | `http://localhost:18082/requests` | 每个请求的模型、状态、token 数、耗时、完成/失败标签 |
| 实时日志 | `http://localhost:18082/` | 带语法高亮的实时日志流 |

**请求监控卡片显示**：
- ✅ 生成完成 / ❌ 失败 / 💤 浏览器空闲回收
- 模式（流式/假流式）
- 输入/输出 token 数（带千分位格式）
- 实际生成耗时
- 错误详情（网络断开、模板缺失等）

启动日志服务：
```bash
node logviewer.js &
```

---

## 稳定性优化

相比上游，本 Fork 在以下方面做了大量稳定性增强：

### BotGuard 握手
| 优化 | 说明 |
|---|---|
| Hook 重试 8×2.5s | 页面 JS 加载慢时多给时间安装拦截器 |
| textarea 等待 30s + 就绪轮询 | 先轮询 DOM 就绪再找输入框，减少误判 |
| textarea 找不到时刷新重试 | 页面渲染异常时自动刷新而不是直接报错 |
| wire template missing 重试 | 第一次没拦截到请求时重新填写并点 Run |
| 坏模板强制重握手 | 检测到不完整的模板 body 立即清缓存重新握手 |
| in-page 模板回退 | CDP 拦截失败时从页面内 JS 变量获取模板 |

### 切号优化
| 优化 | 说明 |
|---|---|
| 跳过冗余导航点击 | 已在 new_chat 页面时不再点 Playground 按钮 |
| evaluate 超时容忍 | 连续 6 次超时才判定卡死，单次偶发不会中断 |
| 卡死后自动刷新 | evaluate 超时达阈值后自动重新导航恢复 |
| V8 堆内存 512MB | 减少 GC 暂停导致的 JS 主线程阻塞 |

### 解析容错
| 优化 | 说明 |
|---|---|
| IndexError/TypeError/KeyError 拦截 | Gemini 响应解析异常时返回友好错误而不是 500 |
| 堆栈诊断日志 | 解析失败时输出完整堆栈和原始数据，方便排查 |
| 清模板缓存 | 解析连续失败时自动清除可能损坏的缓存模板 |

---

## 配置与环境变量

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `AISTUDIO_PORT` | `8080` | API 服务端口 |
| `AISTUDIO_WEB_PASSWORD` | `""` | Web 管理面板密码 |
| `AISTUDIO_PROXY` | `""` | 出口代理（http/https/socks5） |
| `AISTUDIO_DATA_DIR` | `data/` | 数据持久化目录 |
| `AISTUDIO_BROWSER_IDLE_TIMEOUT` | `900` | 浏览器空闲回收超时（秒） |
| `AISTUDIO_DEFAULT_TEXT_MODEL` | `gemini-3.7-flash` | 默认文本模型 |
| `AISTUDIO_BROWSER_HEADLESS` | `true` | 无头模式 |
| `AISTUDIO_DUMP_REQUESTS` | `false` | 调试用：输出请求报文 |

完整环境变量列表请参考上游文档。

---

## API 调用示例

### OpenAI 格式（推荐，对接酒馆/中转站）

```bash
# 普通对话
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-4-argon",
    "stream": true,
    "messages": [
      {"role": "user", "content": "你好"}
    ]
  }'

# 假流式模式
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "假流式-gemini-4-argon",
    "stream": true,
    "messages": [
      {"role": "user", "content": "写一篇长文"}
    ]
  }'

# 抗截断模式
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "抗截断-gemini-4-argon",
    "stream": true,
    "messages": [
      {"role": "user", "content": "写一篇超长小说"}
    ]
  }'
```

### Gemini 原生格式

```bash
# 流式生成
curl http://localhost:8080/v1beta/models/gemini-3.7-flash:streamGenerateContent?alt=sse \
  -H "x-goog-api-key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [{"role": "user", "parts": [{"text": "你好"}]}]
  }'
```

### Python SDK

```python
from google import genai

client = genai.Client(
    api_key="your-api-key",
    http_options={"api_version": "v1beta", "base_url": "http://localhost:8080"},
)

for chunk in client.models.generate_content_stream(
    model="gemini-3.7-flash",
    contents="解释量子计算",
):
    print(chunk.text, end="", flush=True)
```

---

## 常见问题

### BotGuard 握手超时是怎么回事？

BotGuard 是 Google 的反机器人验证。每次切号/启动时需要模拟发一条消息来抓取验证模板。超时通常是因为：

- **页面加载慢**：AI Studio 是 Angular 重应用，JS bundle 很大，手机上的无头浏览器 CPU/内存有限
- **V8 GC 暂停**：内存紧张时垃圾回收会阻塞 JS 主线程数秒
- **切号冷启动**：新 tab 要重新加载所有 JS、初始化 Angular
- **零配额账号**：账号对目标模型没有配额时，点 Run 后请求不会发出

本 Fork 已做了大量容错（自动重试、刷新、增大内存），但在低端设备上偶尔仍可能出现。

### 酒馆显示连接超时？

确保使用了假流式模式（模型名加 `假流式-` 前缀），内置心跳会持续保持连接。同时检查 New API 的超时设置。

### 如何查看请求是否成功？

访问 `http://localhost:18082/requests` 查看请求监控面板，每张卡片显示完成状态、token 数和耗时。

---

## 致谢

- 上游项目 [DeconstructedCube/aistudio-api](https://github.com/DeconstructedCube/aistudio-api)

## 开源协议

[MIT License](./LICENSE)
