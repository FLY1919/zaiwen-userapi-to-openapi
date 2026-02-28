import asyncio
import json
import uuid
from typing import Optional, Dict, Any
from mcp.server import Server
from mcp.types import (
    CallToolResult,
    ListToolsRequest,
    ListToolsResult,
    TextContent,
    Tool,
)
from database import get_latest_token
from auth import validate_token
from image import image_tasks, run_image_generation
from music import music_tasks, run_music_generation
from logger import logger

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
        logger.warning("工具调用可能发生在初始化完成前，但将继续处理")

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
            if not filtered_args.get("prompt"):
                del filtered_args["prompt"]
            if not filtered_args.get("tags"):
                del filtered_args["tags"]
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
        logger.error(f"工具调用异常 {name}: {e}")
        return CallToolResult(
            content=[TextContent(type="text", text=f"错误: {str(e)}")],
            isError=True
        )

async def handle_generate_image_submit(arguments: dict, token: str) -> CallToolResult:
    task_id = uuid.uuid4().hex
    prompt = arguments["prompt"]
    image_asset_id = arguments.get("image_asset_id")
    ratio = arguments.get("ratio", "1:1")

    image_tasks[task_id] = {
        "status": "pending",
        "result": None,
        "error": None,
        "token": token,
        "prompt": prompt,
        "image_asset_id": image_asset_id,
        "ratio": ratio
    }

    asyncio.create_task(run_image_generation(task_id, token, prompt, image_asset_id, ratio))
    logger.info(f"图片任务提交: {task_id}, prompt: {prompt[:50]}...")

    return CallToolResult(
        content=[TextContent(type="text", text=f"任务已提交，任务ID: {task_id}\n请稍后使用 get_image_result 获取结果。")]
    )

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
    else:
        return CallToolResult(
            content=[TextContent(type="text", text=task["result"])]
        )

async def handle_generate_music_submit(arguments: dict, token: str) -> CallToolResult:
    task_id = uuid.uuid4().hex
    title = arguments["title"]
    prompt = arguments.get("prompt", title)
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
    logger.info(f"音乐任务提交: {task_id}, title: {title}")

    return CallToolResult(
        content=[TextContent(type="text", text=f"任务已提交，任务ID: {task_id}\n请稍后使用 get_music_result 获取结果。")]
    )

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
    else:
        return CallToolResult(
            content=[TextContent(type="text", text=task["result"])]
        )