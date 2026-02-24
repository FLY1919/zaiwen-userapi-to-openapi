在问AI 代理服务文档

1. 项目概述

本项目是一个多功能代理服务，它将原始的“在问AI”后端API封装成兼容OpenAI格式的HTTP接口，并同时提供MCP（Model Context Protocol）服务器，支持图片生成和音乐生成的异步任务处理。主要功能包括：

· OpenAI兼容接口：通过 /v1/chat/completions 提供聊天补全，支持流式输出。
· MCP服务器：通过SSE协议提供工具调用，包括图片生成和音乐生成的提交与结果查询。
· 用户登录：提供简单的Web登录页面和API，用于获取和存储访问令牌。
· 文件上传：支持图片上传至七牛云，并返回资产ID供后续使用。

2. 安装依赖

2.1 创建虚拟环境（推荐）

```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate      # Windows
```

2.2 安装所需包

```bash
pip install fastapi uvicorn httpx pydantic python-multipart mcp
```

3. 配置说明

所有配置项集中在代码文件开头的 配置区域，可根据需要修改：

```python
# ========== 配置区域 ==========
BASE_URL = "https://back.zaiwenai.com"          # 原始API地址
HEADERS_TEMPLATE = {...}                         # 请求头模板
DB_PATH = "proxy.db"                              # SQLite数据库路径

# 模型配置
DEFAULT_IMAGE_MODEL = "Flux-2-Pro"                # 图片生成使用的便宜模型
DEFAULT_MUSIC_MODEL = "zaiwen"                    # 音乐生成使用的基础模型

# 任务轮询配置
TASK_TIMEOUT_SECONDS = 1200                        # 最长等待时间 20 分钟
POLL_INTERVAL = 2.0                                # 轮询间隔 2 秒
POLL_MAX_ATTEMPTS = int(TASK_TIMEOUT_SECONDS / POLL_INTERVAL)  # 自动计算轮询次数
# ==============================
```

4. 运行服务

4.1 启动代理服务

```bash
python 2api.py
```

默认监听 http://[::]:8001（IPv6）和 http://127.0.0.1:8001（IPv4）。

4.2 首次登录获取Token

服务启动后，访问 http://localhost:8001/login 进入登录页面，输入手机号、获取验证码并登录。登录成功后Token自动保存到SQLite数据库，后续请求自动携带。

5. OpenAI兼容API

5.1 获取模型列表

```http
GET /v1/models
```

返回可用模型列表（过滤掉纯绘画模型）。

5.2 聊天补全

```http
POST /v1/chat/completions
Content-Type: application/json

{
  "model": "gpt-5-mini",
  "messages": [
    {"role": "user", "content": "你好"}
  ],
  "stream": true,          # 可选，是否流式输出
  "file_ids": []            # 可选，上传的资产ID列表
}
```

支持流式和非流式响应，与OpenAI格式完全兼容。

6. MCP工具使用

服务通过SSE协议提供MCP服务器，端点地址为：http://win-fly.090716.xyz:8001/mcp/sse（请替换为您的实际域名或IP）。

MCP客户端连接后会自动发现以下工具：

6.1 generate_image

提交图片生成任务，立即返回任务ID。

· 参数：
  · prompt (string, 必填): 图片描述提示词
  · image_asset_id (string, 可选): 参考图的资产ID（用于图生图）
  · ratio (string, 可选): 图片比例，如 1:1, 16:9，默认 1:1
· 返回：任务ID字符串

6.2 get_image_result

根据任务ID获取图片生成结果。

· 参数：
  · task_id (string, 必填): 图片生成任务ID
· 返回：Markdown格式的图片（![生成图片](图片URL)），如果任务未完成则返回提示信息。

6.3 generate_music

提交音乐生成任务，立即返回任务ID。

· 参数：
  · title (string, 必填): 歌曲标题
  · prompt (string, 可选): 歌词提示词/内容，留空则AI自动生成
  · tags (string, 可选): 音乐风格标签，如 "trance新流行,进步"
  · make_instrumental (boolean, 可选): 是否生成纯音乐（无歌词），默认 false
· 返回：任务ID字符串

6.4 get_music_result

根据任务ID获取音乐生成结果。

· 参数：
  · task_id (string, 必填): 音乐生成任务ID
· 返回：Markdown格式，包含歌曲标题、试听链接和歌词（如果有）。

7. 文件上传

7.1 上传图片获取资产ID

```http
POST /upload/image
Content-Type: multipart/form-data

file: <图片文件>
```

返回JSON：

```json
{
  "asset_id": "699939bbed8f41163466be65",
  "url": "https://oss.zaiwen.top/xxxx.jpg"
}
```

获取到的 asset_id 可用于图片生成工具的 image_asset_id 参数，实现图生图。

8. 常见问题

8.1 为什么音乐生成任务一直返回“任务正在处理中”？

可能原因：

· 后端处理时间较长（最多20分钟），请耐心等待。
· 任务创建失败，检查日志中是否有 Task not found 错误，如有则可能是原始API临时故障。

8.2 如何查看服务日志？

启动服务后，控制台会输出INFO和错误日志。日志中会显示任务提交、轮询进度和错误详情。

8.3 如何自定义使用的模型？

修改代码开头的 DEFAULT_IMAGE_MODEL 和 DEFAULT_MUSIC_MODEL 为可用模型名称（可通过 /v1/models 获取列表）。

8.4 客户端连接MCP失败？

· 确认服务已启动且端口8001开放。
· 检查防火墙设置，确保外部可访问。
· 客户端配置的URL必须正确指向 /mcp/sse 端点。

8.5 支持哪些MCP客户端？

任何支持MCP协议的应用均可使用，如 Claude Desktop、继续等。配置示例：

```json
{
  "mcpServers": {
    "zaiwen-creative": {
      "url": "http://您的域名:8001/mcp/sse"
    }
  }
}
```

9. 许可证

本项目仅供学习和研究使用，不得用于商业用途。使用前请确保遵守相关服务条款。

---

如有任何问题，请查阅日志或联系开发者。