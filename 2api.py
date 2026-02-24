import asyncio
import json
import sqlite3
import time
import uuid
from typing import List, Optional, Dict, Any, AsyncGenerator

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict

# ---------- MCP 相关导入 ----------
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import (
    CallToolRequest,
    CallToolResult,
    ListToolsRequest,
    ListToolsResult,
    TextContent,
    Tool,
)
from starlette.requests import Request as StarletteRequest

# ========== 配置区域 ==========
BASE_URL = "https://back.zaiwenai.com"
HEADERS_TEMPLATE: Dict[str, Any] = {
    "channel": "web.zaiwenai.com",
    "Origin": "https://www.zaiwenai.com",
    "Referer": "https://www.zaiwenai.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}
DB_PATH = "proxy.db"

# 模型配置
DEFAULT_IMAGE_MODEL = "grok-imagine-image"      # 图片生成使用的便宜模型
DEFAULT_MUSIC_MODEL = "zaiwen"          # 音乐生成使用的基础模型

# 任务轮询配置
TASK_TIMEOUT_SECONDS = 1200              # 最长等待时间 20 分钟
POLL_INTERVAL = 2.0                      # 轮询间隔 2 秒
POLL_MAX_ATTEMPTS = int(TASK_TIMEOUT_SECONDS / POLL_INTERVAL)

# 其他配置
MAX_HISTORY = 200
# ==============================

# ---------- 任务存储（内存） ----------
image_tasks: Dict[str, Dict[str, Any]] = {}
music_tasks: Dict[str, Dict[str, Any]] = {}

# ---------- SQLite 初始化 ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS tokens
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT, created_at INTEGER)''')
    conn.commit()
    conn.close()

init_db()

def get_latest_token() -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT token FROM tokens ORDER BY created_at DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_token(token: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO tokens (token, created_at) VALUES (?, ?)",
              (token, int(time.time())))
    conn.commit()
    conn.close()

def delete_token(token: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM tokens WHERE token=?", (token,))
    conn.commit()
    conn.close()

# ---------- 验证token ----------
async def validate_token(token: str) -> bool:
    headers: Dict[str, Any] = HEADERS_TEMPLATE.copy()
    headers["token"] = token
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{BASE_URL}/api/v1/config/model/chat/", headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("code") == 0
            else:
                return False
        except:
            return False

# ---------- 上传相关 ----------
async def get_upload_token(token: str) -> Dict:
    headers: Dict[str, Any] = HEADERS_TEMPLATE.copy()
    headers["token"] = token
    url = f"{BASE_URL}/api/v1/asset/config"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        data = resp.json()
        if data.get("code") != 0:
            raise HTTPException(status_code=400, detail=data.get("msg"))
        return data["data"]

async def upload_to_qiniu(file_content: bytes, file_name: str, upload_info: Dict) -> str:
    upload_url = f"https://upload-{upload_info['region']}.qiniup.com"
    files = {
        "file": (file_name, file_content, "image/png")
    }
    data = {"token": upload_info["token"]}
    async with httpx.AsyncClient() as client:
        resp = await client.post(upload_url, data=data, files=files)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        result = resp.json()
        return result["key"]

async def add_asset(token: str, name: str, format: str, size: int, owner: str, url: str, thumbnail: str = "") -> str:
    headers: Dict[str, Any] = HEADERS_TEMPLATE.copy()
    headers["token"] = token
    payload = {
        "name": name,
        "format": format,
        "size": size,
        "owner": owner,
        "url": url,
        "thumbnail": thumbnail
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{BASE_URL}/api/v1/asset/add", json=payload, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        data = resp.json()
        if data.get("code") != 0:
            raise HTTPException(status_code=400, detail=data.get("msg"))
        return data["data"]["id"]

# ---------- Pydantic 模型 ----------
class Message(BaseModel):
    role: str
    content: Optional[str] = ""
    model_config = ConfigDict(extra="allow")

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Message]
    stream: Optional[bool] = False
    file_ids: Optional[List[str]] = []
    model_config = ConfigDict(extra="allow")

class ChatCompletionResponseChoice(BaseModel):
    index: int
    message: Message
    finish_reason: Optional[str] = "stop"

class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionResponseChoice]

# ---------- 工具函数 ----------
def merge_messages_to_prompt(messages: List[Dict]) -> str:
    lines = []
    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")
        if role == "system":
            lines.append(f"[System] {content}")
        elif role == "user":
            lines.append(f"[User] {content}")
        elif role == "assistant":
            lines.append(f"[Assistant] {content}")
        else:
            lines.append(f"[{role}] {content}")
    return "\n\n".join(lines)

async def call_original_stream(prompt: str, model_key: str, token: str, file_ids: Optional[List[str]] = None) -> AsyncGenerator[str, None]:
    headers: Dict[str, Any] = HEADERS_TEMPLATE.copy()
    headers["token"] = token
    headers["Content-Type"] = "application/json"

    file_obj = {}
    if file_ids and isinstance(file_ids, list) and file_ids:
        file_obj = {"formats": "media", "ids": file_ids}

    payload = {
        "data": {
            "content": prompt,
            "model": model_key,
            "round": 5,
            "type": "text",
            "online": False,
            "file": file_obj,
            "knowledge": [],
            "draw": {},
            "suno_input": {},
            "video": {
                "ratio": "1:1",
                "original_image": {"image": {}, "weight": 50}
            }
        }
    }

    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream("POST", f"{BASE_URL}/api/v1/ai/message/stream",
                                 json=payload, headers=headers) as response:
            if response.status_code != 200:
                error_text = await response.aread()
                raise HTTPException(status_code=response.status_code, detail=error_text.decode())
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    yield line[6:]

async def delete_conversation(token: str, conversation_id: str):
    """异步删除会话"""
    headers: Dict[str, Any] = HEADERS_TEMPLATE.copy()
    headers["token"] = token
    headers["Content-Type"] = "application/json"
    url = f"{BASE_URL}/api/v1/ai/conversation/delete"
    payload = {"id": conversation_id}
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    print(f"[删除] 会话 {conversation_id} 已删除")
                else:
                    print(f"[删除失败] {data.get('msg')}")
            else:
                print(f"[删除HTTP错误] {resp.status_code}")
        except Exception as e:
            print(f"[删除异常] {e}")

async def original_to_openai_stream_with_cleanup(
    original_gen: AsyncGenerator[str, None],
    token: str
) -> AsyncGenerator[str, None]:
    """转换原始SSE为OpenAI流式格式，并在结束时删除会话"""
    conversation_id = None
    try:
        async for event_str in original_gen:
            try:
                event = json.loads(event_str)
                if event.get("type") == "conversation":
                    conversation_id = event.get("data", {}).get("id")
            except:
                pass

            try:
                event = json.loads(event_str)
            except:
                continue
            if event.get("type") == "streaming":
                content = event.get("content", "")
                chunk = {
                    "id": f"chatcmpl-{uuid.uuid4().hex}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "unknown",
                    "choices": [{
                        "index": 0,
                        "delta": {"content": content},
                        "finish_reason": None
                    }]
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        final_chunk = {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "unknown",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop"
            }]
        }
        yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        if conversation_id:
            asyncio.create_task(delete_conversation(token, conversation_id))

# ---------- 图片生成相关 ----------
async def call_original_draw_stream(prompt: str, model_key: str, token: str,
                                     image_asset_id: Optional[str] = None,
                                     ratio: str = "1:1") -> AsyncGenerator[str, None]:
    headers: Dict[str, Any] = HEADERS_TEMPLATE.copy()
    headers["token"] = token
    headers["Content-Type"] = "application/json"

    draw_obj = {"ratio": ratio}
    if image_asset_id:
        draw_obj["original_image"] = {"asset": image_asset_id, "weight": 1}

    payload = {
        "data": {
            "content": prompt,
            "model": model_key,
            "round": 5,
            "type": "draw",
            "online": False,
            "file": {},
            "knowledge": [],
            "draw": draw_obj,
            "suno_input": {},
            "video": {
                "ratio": "1:1",
                "original_image": {"image": {}, "weight": 50}
            }
        }
    }

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            async with client.stream("POST", f"{BASE_URL}/api/v1/ai/message/stream",
                                     json=payload, headers=headers) as response:
                if response.status_code != 200:
                    error_text = await response.aread()
                    print(f"[绘画请求失败] HTTP {response.status_code}: {error_text.decode()}")
                    raise Exception(f"绘画请求失败: HTTP {response.status_code}")
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        yield line[6:]
        except httpx.HTTPStatusError as e:
            print(f"[绘画请求HTTP错误] {e}")
            raise Exception(f"绘画请求HTTP错误: {e}")
        except Exception as e:
            print(f"[绘画请求异常] {e}")
            raise

async def poll_draw_task(task_id: str, token: str, max_attempts: int = POLL_MAX_ATTEMPTS, interval: float = POLL_INTERVAL) -> Dict:
    headers: Dict[str, Any] = HEADERS_TEMPLATE.copy()
    headers["token"] = token
    url = f"{BASE_URL}/api/v1/draw/task"
    for attempt in range(max_attempts):
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url, params={"task": task_id}, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("code") == 0:
                        status = data["data"]["status"]
                        if status == "completed":
                            return data["data"]
                        elif status == "failed":
                            error_detail = data.get("data", {}).get("error", "未知错误")
                            raise Exception(f"绘画任务失败: {error_detail}")
                    else:
                        # 业务错误，例如任务不存在
                        if data.get("code") in ("02404", 404):
                            raise Exception(f"任务不存在: {data.get('msg')}")
                        raise Exception(f"查询任务失败: {data.get('msg')}")
                else:
                    error_text = await resp.aread()
                    print(f"[轮询绘画任务] HTTP {resp.status_code}: {error_text.decode()}")
                    if resp.status_code == 404:
                        raise Exception(f"任务不存在 (HTTP 404)")
                    if attempt == max_attempts - 1:
                        raise Exception(f"HTTP错误: {resp.status_code}")
            except Exception as e:
                print(f"[轮询绘画任务异常] attempt {attempt+1}: {e}")
                if "不存在" in str(e) or "02404" in str(e) or "404" in str(e):
                    raise
                if attempt == max_attempts - 1:
                    raise
        await asyncio.sleep(interval)
    raise Exception("绘画任务超时")

# ---------- 音乐生成相关 ----------
async def call_original_suno_stream(prompt: str, title: str, token: str,
                                     model: str = DEFAULT_MUSIC_MODEL, 
                                     tags: Optional[str] = None,
                                     make_instrumental: bool = False) -> AsyncGenerator[str, None]:
    headers: Dict[str, Any] = HEADERS_TEMPLATE.copy()
    headers["token"] = token
    headers["Content-Type"] = "application/json"

    suno_input = {"title": title, "make_instrumental": make_instrumental}
    if tags:
        suno_input["tags"] = tags

    payload = {
        "data": {
            "content": prompt,
            "model": model,
            "round": 5,
            "type": "suno",
            "online": False,
            "file": {},
            "knowledge": [],
            "draw": {},
            "suno_input": suno_input,
            "video": {
                "ratio": "1:1",
                "original_image": {"image": {}, "weight": 50}
            }
        }
    }

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            async with client.stream("POST", f"{BASE_URL}/api/v1/ai/message/stream",
                                     json=payload, headers=headers) as response:
                if response.status_code != 200:
                    error_text = await response.aread()
                    print(f"[音乐请求失败] HTTP {response.status_code}: {error_text.decode()}")
                    raise Exception(f"音乐生成请求失败: HTTP {response.status_code}")
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        yield line[6:]
        except Exception as e:
            print(f"[音乐请求异常] {e}")
            raise

async def poll_suno_task(task_id: str, token: str, max_attempts: int = POLL_MAX_ATTEMPTS, interval: float = POLL_INTERVAL) -> Dict:
    headers: Dict[str, Any] = HEADERS_TEMPLATE.copy()
    headers["token"] = token
    url = f"{BASE_URL}/api/v1/suno/task"
    for attempt in range(max_attempts):
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url, params={"task": task_id}, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("code") == 0:
                        status = data["data"]["status"]
                        if status == "completed":
                            return data["data"]
                        elif status == "failed":
                            error_detail = data.get("data", {}).get("error", "未知错误")
                            raise Exception(f"音乐任务失败: {error_detail}")
                    else:
                        # 业务错误，例如任务不存在
                        if data.get("code") in ("02404", 404):
                            raise Exception(f"任务不存在: {data.get('msg')}")
                        raise Exception(f"查询任务失败: {data.get('msg')}")
                else:
                    error_text = await resp.aread()
                    print(f"[轮询音乐任务] HTTP {resp.status_code}: {error_text.decode()}")
                    if resp.status_code == 404:
                        raise Exception(f"任务不存在 (HTTP 404)")
                    if attempt == max_attempts - 1:
                        raise Exception(f"HTTP错误: {resp.status_code}")
            except Exception as e:
                print(f"[轮询音乐任务异常] attempt {attempt+1}: {e}")
                if "不存在" in str(e) or "02404" in str(e) or "404" in str(e):
                    raise
                if attempt == max_attempts - 1:
                    raise
        await asyncio.sleep(interval)
    raise Exception("音乐任务超时")

# ---------- MCP 服务器集成 ----------
server_initialized = False
mcp_server = Server("zaiwen-creative")

@mcp_server.list_tools()
async def list_tools(request: Optional[ListToolsRequest] = None) -> ListToolsResult:
    return ListToolsResult(tools=[
        Tool(
            name="generate_image",
            description="提交图片生成任务，返回任务ID。任务完成后可用 get_image_result 获取结果。\n"
                        "参数说明：\n"
                        "- prompt: 图片描述提示词（必填）\n"
                        "- image_asset_id: 参考图的资产ID（用于图生图，可选）\n"
                        "- ratio: 图片比例，如 1:1, 16:9 等（可选，默认 1:1）",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "图片描述提示词（必填）"},
                    "image_asset_id": {"type": "string", "description": "参考图的资产ID，用于图生图（可选）"},
                    "ratio": {"type": "string", "description": "图片比例，如 1:1, 16:9 等（可选，默认 1:1）", "default": "1:1"}
                },
                "required": ["prompt"],
                "additionalProperties": False
            }
        ),
        Tool(
            name="get_image_result",
            description="根据任务ID获取图片生成结果。返回 Markdown 格式的图片。",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "图片生成任务ID"}
                },
                "required": ["task_id"],
                "additionalProperties": False
            }
        ),
        Tool(
            name="generate_music",
            description="提交音乐生成任务，返回任务ID。任务完成后可用 get_music_result 获取结果。\n"
                        "参数说明：\n"
                        "- title: 歌曲标题（必填）\n"
                        "- prompt: 歌词提示词/内容（可选，留空则 AI 自动生成）\n"
                        "- tags: 音乐风格标签，如 'trance新流行,进步'（可选）\n"
                        "- make_instrumental: 是否生成纯音乐（无歌词），默认为 false",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "歌曲标题（必填）"},
                    "prompt": {"type": "string", "description": "歌词提示词/内容，留空则 AI 自动生成（可选）", "default": ""},
                    "tags": {"type": "string", "description": "音乐风格标签，如 'trance新流行,进步'（可选）"},
                    "make_instrumental": {"type": "boolean", "description": "是否生成纯音乐（无歌词），默认为 false", "default": False}
                },
                "required": ["title"],
                "additionalProperties": False
            }
        ),
        Tool(
            name="get_music_result",
            description="根据任务ID获取音乐生成结果。返回 Markdown 格式的音频链接和歌词。",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "音乐生成任务ID"}
                },
                "required": ["task_id"],
                "additionalProperties": False
            }
        )
    ])

@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    global server_initialized
    if not server_initialized:
        print("[MCP] 警告：工具调用可能发生在初始化完成前，但将继续处理")

    token = get_latest_token()
    if not token:
        return CallToolResult(
            content=[TextContent(type="text", text="请先登录（运行登录脚本或访问 /login）")],
            isError=True
        )
    if not await validate_token(token):
        return CallToolResult(
            content=[TextContent(type="text", text="Token 已失效，请重新登录")],
            isError=True
        )

    try:
        if name == "generate_image":
            # 过滤参数
            filtered_args = {
                "prompt": arguments.get("prompt"),
                "image_asset_id": arguments.get("image_asset_id"),
                "ratio": arguments.get("ratio", "1:1")
            }
            if filtered_args.get("image_asset_id") is None:
                del filtered_args["image_asset_id"]
            return await handle_generate_image_submit(filtered_args, token)

        elif name == "get_image_result":
            task_id = arguments.get("task_id")
            if not task_id:
                return CallToolResult(
                    content=[TextContent(type="text", text="缺少 task_id 参数")],
                    isError=True
                )
            return await handle_get_image_result(task_id)

        elif name == "generate_music":
            filtered_args = {
                "title": arguments.get("title"),
                "prompt": arguments.get("prompt", ""),
                "tags": arguments.get("tags"),
                "make_instrumental": arguments.get("make_instrumental", False)
            }
            # 移除值为 None 或空字符串的可选参数
            if not filtered_args.get("prompt"):
                del filtered_args["prompt"]
            if not filtered_args.get("tags"):
                del filtered_args["tags"]
            # make_instrumental 即使为 False 也保留
            return await handle_generate_music_submit(filtered_args, token)

        elif name == "get_music_result":
            task_id = arguments.get("task_id")
            if not task_id:
                return CallToolResult(
                    content=[TextContent(type="text", text="缺少 task_id 参数")],
                    isError=True
                )
            return await handle_get_music_result(task_id)

        else:
            return CallToolResult(
                content=[TextContent(type="text", text=f"未知工具: {name}")],
                isError=True
            )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return CallToolResult(
            content=[TextContent(type="text", text=f"错误: {str(e)}")],
            isError=True
        )

async def handle_generate_image_submit(arguments: dict, token: str) -> CallToolResult:
    task_id = uuid.uuid4().hex
    prompt = arguments["prompt"]
    image_asset_id = arguments.get("image_asset_id")
    ratio = arguments.get("ratio", "1:1")

    # 存储初始状态
    image_tasks[task_id] = {
        "status": "pending",
        "result": None,
        "error": None,
        "token": token,
        "prompt": prompt,
        "image_asset_id": image_asset_id,
        "ratio": ratio
    }

    # 启动后台任务
    asyncio.create_task(run_image_generation(task_id, token, prompt, image_asset_id, ratio))

    return CallToolResult(
        content=[TextContent(type="text", text=f"任务已提交，任务ID: {task_id}\n请稍后使用 get_image_result 获取结果。")]
    )

async def run_image_generation(task_id: str, token: str, prompt: str, image_asset_id: Optional[str], ratio: str):
    try:
        conversation_id = None
        task_id_remote = None
        async for event_str in call_original_draw_stream(prompt, DEFAULT_IMAGE_MODEL, token, image_asset_id, ratio):
            try:
                event = json.loads(event_str)
            except:
                continue
            if event.get("type") == "conversation":
                conversation_id = event.get("data", {}).get("id")
            elif event.get("type") == "assistant-message":
                draw_result = event.get("data", {}).get("draw_result")
                if draw_result and draw_result.get("task_id"):
                    task_id_remote = draw_result["task_id"]
                    break

        if not task_id_remote:
            raise Exception("未能获取绘画任务 ID")

        result = await poll_draw_task(task_id_remote, token)
        if conversation_id:
            asyncio.create_task(delete_conversation(token, conversation_id))

        images = result.get("images", [])
        if not images:
            raise Exception("绘画完成但未返回图片")
        first_image = images[0]
        image_url = first_image["url"]
        markdown = f"![生成图片]({image_url})"
        image_tasks[task_id]["status"] = "completed"
        image_tasks[task_id]["result"] = markdown
    except Exception as e:
        image_tasks[task_id]["status"] = "failed"
        image_tasks[task_id]["error"] = str(e)
        print(f"[后台任务失败] task_id={task_id}, error={e}")

async def handle_get_image_result(task_id: str) -> CallToolResult:
    task = image_tasks.get(task_id)
    if not task:
        return CallToolResult(
            content=[TextContent(type="text", text=f"任务ID {task_id} 不存在")],
            isError=True
        )
    if task["status"] == "pending":
        return CallToolResult(
            content=[TextContent(type="text", text="任务正在处理中，请稍后重试")]
        )
    elif task["status"] == "failed":
        return CallToolResult(
            content=[TextContent(type="text", text=f"任务失败: {task['error']}")],
            isError=True
        )
    else:  # completed
        return CallToolResult(
            content=[TextContent(type="text", text=task["result"])]
        )

async def handle_generate_music_submit(arguments: dict, token: str) -> CallToolResult:
    task_id = uuid.uuid4().hex
    title = arguments["title"]
    prompt = arguments.get("prompt", title)  # 如果未提供 prompt，用标题作为内容
    tags = arguments.get("tags")
    make_instrumental = arguments.get("make_instrumental", False)

    music_tasks[task_id] = {
        "status": "pending",
        "result": None,
        "error": None,
        "token": token,
        "title": title,
        "prompt": prompt,
        "tags": tags,
        "make_instrumental": make_instrumental
    }

    asyncio.create_task(run_music_generation(task_id, token, title, prompt, tags, make_instrumental))

    return CallToolResult(
        content=[TextContent(type="text", text=f"任务已提交，任务ID: {task_id}\n请稍后使用 get_music_result 获取结果。")]
    )

async def run_music_generation(task_id: str, token: str, title: str, prompt: str, tags: Optional[str], make_instrumental: bool):
    try:
        conversation_id = None
        task_id_remote = None
        async for event_str in call_original_suno_stream(prompt, title, token, DEFAULT_MUSIC_MODEL, tags, make_instrumental):
            try:
                event = json.loads(event_str)
            except:
                continue
            if event.get("type") == "conversation":
                conversation_id = event.get("data", {}).get("id")
            elif event.get("type") == "assistant-message":
                suno_result = event.get("data", {}).get("suno_result")
                if suno_result and suno_result.get("task_id"):
                    task_id_remote = suno_result["task_id"]
                    break

        if not task_id_remote:
            raise Exception("未能获取音乐生成任务 ID")

        result = await poll_suno_task(task_id_remote, token)
        if conversation_id:
            asyncio.create_task(delete_conversation(token, conversation_id))

        data = result.get("data", [])
        if not data:
            raise Exception("音乐生成完成但未返回数据")

        song = data[0]
        audio_url = song.get("audio_url")
        title_out = song.get("title", title)
        lyrics = song.get("prompt", "")

        md_parts = [f"## {title_out}"]
        if audio_url:
            md_parts.append(f"[🎵 试听歌曲]({audio_url})")
        if lyrics:
            md_parts.append("\n### 歌词\n")
            md_parts.append(lyrics)
        markdown = "\n\n".join(md_parts)

        music_tasks[task_id]["status"] = "completed"
        music_tasks[task_id]["result"] = markdown
    except Exception as e:
        music_tasks[task_id]["status"] = "failed"
        music_tasks[task_id]["error"] = str(e)
        print(f"[后台任务失败] task_id={task_id}, error={e}")

async def handle_get_music_result(task_id: str) -> CallToolResult:
    task = music_tasks.get(task_id)
    if not task:
        return CallToolResult(
            content=[TextContent(type="text", text=f"任务ID {task_id} 不存在")],
            isError=True
        )
    if task["status"] == "pending":
        return CallToolResult(
            content=[TextContent(type="text", text="任务正在处理中，请稍后重试")]
        )
    elif task["status"] == "failed":
        return CallToolResult(
            content=[TextContent(type="text", text=f"任务失败: {task['error']}")],
            isError=True
        )
    else:  # completed
        return CallToolResult(
            content=[TextContent(type="text", text=task["result"])]
        )

# 创建 SSE 传输对象
sse_transport = SseServerTransport("/mcp/messages")

# ---------- FastAPI 应用 ----------
app = FastAPI(title="在问AI Proxy")

# ---------- 异常处理器 ----------
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    try:
        body = await request.body()
        body_str = body.decode('utf-8', errors='ignore')
    except:
        body_str = "<无法读取请求体>"

    print("="*50)
    print("请求验证错误")
    print(f"请求体: {body_str}")
    print(f"错误详情: {exc.errors()}")
    print("="*50)

    return JSONResponse(
        status_code=422,
        content={
            "detail": exc.errors(),
            "body": body_str
        }
    )

# ---------- 发送短信验证码 ----------
@app.post("/send-sms")
async def send_sms(phone: str = Form(...)):
    headers: Dict[str, Any] = HEADERS_TEMPLATE.copy()
    url = f"{BASE_URL}/api/v1/user/sms"
    payload = {"phone": phone}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        data = resp.json()
        if data.get("code") != "0":
            raise HTTPException(status_code=400, detail=data.get("msg"))
        return {"message": data.get("data", "验证码发送成功")}

# ---------- 登录页面 ----------
@app.get("/login", response_class=HTMLResponse)
async def login_page():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>登录</title>
        <meta charset="utf-8">
        <style>
            body { font-family: Arial; padding: 20px; max-width: 400px; margin: auto; }
            input { width: 100%; padding: 8px; margin: 5px 0; box-sizing: border-box; }
            button { padding: 10px; margin-top: 10px; width: 100%; }
            #sms-btn { background-color: #4CAF50; color: white; border: none; cursor: pointer; }
            #sms-btn:disabled { background-color: #cccccc; cursor: not-allowed; }
            #message { margin-top: 10px; color: green; }
        </style>
    </head>
    <body>
        <h2>在问AI 登录</h2>
        <form id="loginForm">
            <label>手机号:</label><br>
            <input type="text" id="phone" name="phone" required><br>
            <button type="button" id="sms-btn" onclick="sendSMS()">获取验证码</button><br>
            <label>验证码:</label><br>
            <input type="text" id="code" name="code" required><br>
            <label>邀请码 (可选):</label><br>
            <input type="text" id="invite_code" name="invite_code"><br>
            <button type="submit">登录</button>
        </form>
        <div id="message"></div>
        <p><a href="/">返回测试页面</a></p>

        <script>
            let countdown = 0;
            const smsBtn = document.getElementById('sms-btn');
            const phoneInput = document.getElementById('phone');
            const messageDiv = document.getElementById('message');

            async function sendSMS() {
                const phone = phoneInput.value.trim();
                if (!phone) {
                    alert('请输入手机号');
                    return;
                }
                smsBtn.disabled = true;
                let seconds = 60;
                smsBtn.textContent = `已发送(${seconds}s)`;
                const interval = setInterval(() => {
                    seconds--;
                    smsBtn.textContent = `已发送(${seconds}s)`;
                    if (seconds <= 0) {
                        clearInterval(interval);
                        smsBtn.disabled = false;
                        smsBtn.textContent = '获取验证码';
                    }
                }, 1000);

                try {
                    const formData = new FormData();
                    formData.append('phone', phone);
                    const res = await fetch('/send-sms', {
                        method: 'POST',
                        body: formData
                    });
                    if (!res.ok) {
                        const err = await res.text();
                        throw new Error(err);
                    }
                    const data = await res.json();
                    messageDiv.textContent = data.message;
                } catch (e) {
                    messageDiv.textContent = '发送失败：' + e.message;
                    clearInterval(interval);
                    smsBtn.disabled = false;
                    smsBtn.textContent = '获取验证码';
                }
            }

            document.getElementById('loginForm').addEventListener('submit', async function(e) {
                e.preventDefault();
                const phone = document.getElementById('phone').value.trim();
                const code = document.getElementById('code').value.trim();
                const invite = document.getElementById('invite_code').value.trim();

                if (!phone || !code) {
                    alert('手机号和验证码不能为空');
                    return;
                }

                const formData = new FormData();
                formData.append('phone', phone);
                formData.append('code', code);
                formData.append('invite_code', invite);

                try {
                    const res = await fetch('/login', {
                        method: 'POST',
                        body: formData
                    });
                    if (!res.ok) {
                        const err = await res.text();
                        throw new Error(err);
                    }
                    const data = await res.json();
                    alert('登录成功！');
                    window.location.href = '/';
                } catch (e) {
                    messageDiv.textContent = '登录失败：' + e.message;
                }
            });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

@app.post("/login")
async def login_post(phone: str = Form(...), code: str = Form(...), invite_code: str = Form("")):
    headers: Dict[str, Any] = HEADERS_TEMPLATE.copy()
    url = f"{BASE_URL}/api/v1/user/login"
    payload = {
        "phone": phone,
        "code": code,
        "inviteCode": invite_code
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        token = resp.headers.get("token")
        if not token:
            raise HTTPException(status_code=400, detail="No token in response")
        data = resp.json()
        if data.get("code") != "0":
            raise HTTPException(status_code=400, detail=data.get("msg"))
        save_token(token)
        return {"message": "登录成功", "token": token[:20] + "..."}

# ---------- 图片上传 ----------
@app.post("/upload/image")
async def upload_image(file: UploadFile = File(...)):
    token = get_latest_token()
    if not token:
        raise HTTPException(status_code=401, detail="请先登录")

    if not await validate_token(token):
        delete_token(token)
        raise HTTPException(status_code=401, detail="Token 已失效，请重新登录")

    try:
        content = await file.read()
        size = len(content)
        content_type = file.content_type or "image/png"
        filename = file.filename or "image.png"

        upload_info = await get_upload_token(token)
        key = await upload_to_qiniu(content, filename, upload_info)

        headers: Dict[str, Any] = HEADERS_TEMPLATE.copy()
        headers["token"] = token
        async with httpx.AsyncClient() as client:
            try:
                user_resp = await client.get(f"{BASE_URL}/api/v1/user/token", headers=headers, timeout=5)
                if user_resp.status_code == 200:
                    user_data = user_resp.json()
                    owner = user_data.get("data", {}).get("uid", "")
                else:
                    owner = ""
            except Exception:
                owner = ""

        asset_id = await add_asset(token, filename, content_type, size, owner, key, key)
        return {"asset_id": asset_id, "url": f"{upload_info['domain']}/{key}"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] 图片上传未知错误: {e}")
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {str(e)}")

# ---------- 模型列表 ----------
@app.get("/v1/models")
async def list_models():
    token = get_latest_token()
    if not token:
        raise HTTPException(status_code=401, detail="No token, please login first")
    if not await validate_token(token):
        delete_token(token)
        raise HTTPException(status_code=401, detail="Token expired or invalid")

    headers: Dict[str, Any] = HEADERS_TEMPLATE.copy()
    headers["token"] = token
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/api/v1/config/model/chat/", headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        data = resp.json()
        if data.get("code") != 0:
            raise HTTPException(status_code=400, detail=data.get("msg"))

        models = []
        for item in data.get("data", []):
            if "pic" in item.get("tags", []):
                continue
            models.append({
                "id": item["key"],
                "object": "model",
                "created": int(time.time()),
                "owned_by": "organization"
            })
        return {"object": "list", "data": models}

# ---------- 聊天补全 ----------
@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    print(f"[请求] model={request.model}, stream={request.stream}, file_ids={request.file_ids}")
    token = get_latest_token()
    if not token:
        raise HTTPException(status_code=401, detail="No token, please login first")
    if not await validate_token(token):
        delete_token(token)
        raise HTTPException(status_code=401, detail="Token expired or invalid")

    file_ids = request.file_ids
    if file_ids is not None:
        if not isinstance(file_ids, list):
            print(f"[警告] file_ids 类型错误: {type(file_ids)}，将设为空列表")
            file_ids = []
        else:
            file_ids = [fid for fid in file_ids if isinstance(fid, str)]

    messages_dict = [msg.model_dump() for msg in request.messages]  # 使用 model_dump 替代 dict
    prompt = merge_messages_to_prompt(messages_dict)

    try:
        original_gen = call_original_stream(prompt, request.model, token, file_ids)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if request.stream:
        return StreamingResponse(
            original_to_openai_stream_with_cleanup(original_gen, token),
            media_type="text/event-stream"
        )
    else:
        full_content = ""
        conversation_id = None
        async for event_str in original_gen:
            try:
                event = json.loads(event_str)
                if event.get("type") == "conversation":
                    conversation_id = event.get("data", {}).get("id")
                elif event.get("type") == "streaming":
                    full_content += event.get("content", "")
            except:
                continue
        response_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        response = ChatCompletionResponse(
            id=response_id,
            created=created,
            model=request.model,
            choices=[
                ChatCompletionResponseChoice(
                    index=0,
                    message=Message(role="assistant", content=full_content),
                    finish_reason="stop"
                )
            ]
        )
        if conversation_id:
            asyncio.create_task(delete_conversation(token, conversation_id))
        return response

# ---------- 测试页面 ----------
@app.get("/", response_class=HTMLResponse)
async def test_page():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>在问AI 测试页</title>
        <meta charset="utf-8">
        <style>
            body { font-family: Arial; padding: 20px; max-width: 800px; margin: auto; }
            #models { width: 100%; padding: 8px; margin-bottom: 10px; }
            #message { width: 100%; height: 80px; padding: 8px; margin-bottom: 10px; }
            #file { margin-bottom: 10px; }
            #send { padding: 10px 20px; background: #4CAF50; color: white; border: none; cursor: pointer; }
            #response { margin-top: 20px; border: 1px solid #ddd; padding: 10px; min-height: 100px; white-space: pre-wrap; }
            .image-preview { max-width: 200px; max-height: 200px; margin-top: 10px; }
        </style>
    </head>
    <body>
        <h2>在问AI 测试页面</h2>
        <p><a href="/login">登录</a>（如果尚未登录）</p>

        <label for="models">选择模型：</label>
        <select id="models">
            <option value="">加载中...</option>
        </select><br>

        <label for="message">消息内容：</label>
        <textarea id="message" placeholder="输入消息..."></textarea>

        <label for="file">上传图片（可选）：</label>
        <input type="file" id="file" accept="image/*"><br>
        <div id="preview"></div>

        <label>
            <input type="checkbox" id="streamCheckbox" checked> 流式输出
        </label><br>

        <button id="send">发送</button>

        <div id="response"></div>

        <script>
            const modelSelect = document.getElementById('models');
            const messageInput = document.getElementById('message');
            const fileInput = document.getElementById('file');
            const previewDiv = document.getElementById('preview');
            const sendBtn = document.getElementById('send');
            const responseDiv = document.getElementById('response');
            const streamCheckbox = document.getElementById('streamCheckbox');

            async function loadModels() {
                try {
                    const res = await fetch('/v1/models');
                    if (!res.ok) throw new Error(await res.text());
                    const data = await res.json();
                    modelSelect.innerHTML = '';
                    data.data.forEach(m => {
                        const option = document.createElement('option');
                        option.value = m.id;
                        option.textContent = m.id;
                        modelSelect.appendChild(option);
                    });
                } catch (e) {
                    modelSelect.innerHTML = '<option value="">加载失败</option>';
                    console.error(e);
                }
            }
            loadModels();

            fileInput.addEventListener('change', function(e) {
                const file = e.target.files[0];
                if (file) {
                    const reader = new FileReader();
                    reader.onload = function(ev) {
                        previewDiv.innerHTML = `<img src="${ev.target.result}" class="image-preview">`;
                    }
                    reader.readAsDataURL(file);
                } else {
                    previewDiv.innerHTML = '';
                }
            });

            async function uploadImage(file) {
                const formData = new FormData();
                formData.append('file', file);
                const res = await fetch('/upload/image', {
                    method: 'POST',
                    body: formData
                });
                if (!res.ok) throw new Error(await res.text());
                const data = await res.json();
                return data.asset_id;
            }

            sendBtn.addEventListener('click', async function() {
                const model = modelSelect.value;
                const content = messageInput.value.trim();
                const file = fileInput.files[0];
                const useStream = streamCheckbox.checked;

                if (!model) {
                    alert('请选择模型');
                    return;
                }
                if (!content && !file) {
                    alert('请输入消息或上传图片');
                    return;
                }

                responseDiv.textContent = '发送中...';
                sendBtn.disabled = true;

                try {
                    let fileIds = [];
                    if (file) {
                        const assetId = await uploadImage(file);
                        fileIds = [assetId];
                    }

                    const messages = [{ role: 'user', content: content || ' ' }];
                    const payload = {
                        model: model,
                        messages: messages,
                        stream: useStream,
                        file_ids: fileIds
                    };

                    console.log('发送payload:', JSON.stringify(payload, null, 2));

                    const res = await fetch('/v1/chat/completions', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });

                    if (!res.ok) {
                        const errText = await res.text();
                        console.error('响应错误:', errText);
                        throw new Error(errText);
                    }

                    if (useStream) {
                        const reader = res.body.getReader();
                        const decoder = new TextDecoder();
                        let fullText = '';
                        responseDiv.innerHTML = '';

                        while (true) {
                            const { done, value } = await reader.read();
                            if (done) break;
                            const chunk = decoder.decode(value);
                            const lines = chunk.split('\\n');
                            for (const line of lines) {
                                if (line.startsWith('data: ')) {
                                    const data = line.slice(6);
                                    if (data === '[DONE]') continue;
                                    try {
                                        const parsed = JSON.parse(data);
                                        const delta = parsed.choices[0].delta.content;
                                        if (delta) {
                                            fullText += delta;
                                            responseDiv.innerHTML = fullText.replace(/\\n/g, '<br>');
                                        }
                                    } catch (e) {}
                                }
                            }
                        }
                    } else {
                        const data = await res.json();
                        responseDiv.innerHTML = data.choices[0].message.content.replace(/\\n/g, '<br>');
                    }
                } catch (e) {
                    responseDiv.textContent = '错误：' + e.message;
                } finally {
                    sendBtn.disabled = false;
                }
            });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

# ---------- MCP SSE 端点 ----------
@app.get("/mcp/sse")
async def mcp_sse(request: StarletteRequest):
    global server_initialized
    server_initialized = False
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as (read_stream, write_stream):
        async def run_server():
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options()
            )
        task = asyncio.create_task(run_server())
        try:
            # 等待一小段时间让初始化完成
            await asyncio.sleep(0.1)
            server_initialized = True
            print("[MCP] 服务器初始化完成")
            await task
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            server_initialized = False

# 创建 ASGI 应用处理 /mcp/messages
async def mcp_messages_app(scope, receive, send):
    await sse_transport.handle_post_message(scope, receive, send)

# 挂载到 /mcp/messages 路径
app.mount("/mcp/messages", mcp_messages_app)

if __name__ == "__main__":
    uvicorn.run(app, host="::", port=8001)