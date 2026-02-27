import asyncio
import json
import uuid
from typing import Optional, Dict, Any, AsyncGenerator
import httpx
from config import HEADERS_TEMPLATE, BASE_URL, DEFAULT_MUSIC_MODEL, POLL_MAX_ATTEMPTS, POLL_INTERVAL
from utils import delete_conversation

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

# 任务存储（由 mcp_server 管理）
music_tasks: Dict[str, Dict[str, Any]] = {}

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