import asyncio
import json
import uuid
from typing import Optional, Dict, Any, AsyncGenerator
import httpx
from config import HEADERS_TEMPLATE, BASE_URL, DEFAULT_IMAGE_MODEL, POLL_MAX_ATTEMPTS, POLL_INTERVAL
from utils import delete_conversation
from logger import logger

# 任务存储（由 mcp_server 管理）
image_tasks: Dict[str, Dict[str, Any]] = {}

async def call_original_draw_stream(prompt: str, model_key: str, token: str,
                                     image_asset_id: Optional[str] = None,
                                     ratio: str = "1:1") -> AsyncGenerator[str, None]:
    headers = HEADERS_TEMPLATE.copy()
    headers["token"] = token
    headers["Content-Type"] = "application/json"

    draw_obj: Dict[str, Any] = {"ratio": ratio}
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
                    logger.error(f"绘画请求失败 HTTP {response.status_code}: {error_text.decode()}")
                    raise Exception(f"绘画请求失败: HTTP {response.status_code}")
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        yield line[6:]
        except httpx.HTTPStatusError as e:
            logger.error(f"绘画请求HTTP错误: {e}")
            raise Exception(f"绘画请求HTTP错误: {e}")
        except Exception as e:
            logger.error(f"绘画请求异常: {e}")
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
                            logger.info(f"绘画任务 {task_id} 完成")
                            return data["data"]
                        elif status == "failed":
                            error_detail = data.get("data", {}).get("error", "未知错误")
                            logger.error(f"绘画任务失败: {error_detail}")
                            raise Exception(f"绘画任务失败: {error_detail}")
                    else:
                        if data.get("code") in ("02404", 404):
                            logger.error(f"绘画任务不存在: {data.get('msg')}")
                            raise Exception(f"任务不存在: {data.get('msg')}")
                        logger.error(f"查询绘画任务失败: {data.get('msg')}")
                        raise Exception(f"查询任务失败: {data.get('msg')}")
                else:
                    error_text = await resp.aread()
                    logger.error(f"轮询绘画任务 HTTP {resp.status_code}: {error_text.decode()}")
                    if resp.status_code == 404:
                        raise Exception(f"任务不存在 (HTTP 404)")
                    if attempt == max_attempts - 1:
                        raise Exception(f"HTTP错误: {resp.status_code}")
            except Exception as e:
                logger.error(f"轮询绘画任务异常 attempt {attempt+1}: {e}")
                if "不存在" in str(e) or "02404" in str(e) or "404" in str(e):
                    raise
                if attempt == max_attempts - 1:
                    raise
        await asyncio.sleep(interval)
    raise Exception("绘画任务超时")

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
        logger.info(f"图片生成任务 {task_id} 完成")
    except Exception as e:
        image_tasks[task_id]["status"] = "failed"
        image_tasks[task_id]["error"] = str(e)
        logger.error(f"图片生成后台任务失败 task_id={task_id}, error={e}")