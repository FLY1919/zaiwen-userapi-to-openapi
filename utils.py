import asyncio
import json
import uuid
from typing import List, Optional, Dict, Any, AsyncGenerator
import httpx
from fastapi import HTTPException
from config import HEADERS_TEMPLATE, BASE_URL, MAX_HISTORY
from database import get_latest_token
from auth import validate_token
import time

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