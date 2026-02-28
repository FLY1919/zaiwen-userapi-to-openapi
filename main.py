import asyncio
import json
import uuid
import time  # 添加 time 模块
from pathlib import Path
from typing import Dict, Any, List

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse  # 添加 StreamingResponse
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request as StarletteRequest
from fastapi.templating import Jinja2Templates
from mcp.server.sse import SseServerTransport

from config import HEADERS_TEMPLATE, BASE_URL, PORT, HOST
from database import init_db, get_latest_token, delete_token
from auth import validate_token, send_sms, login
from upload import get_upload_token, upload_to_qiniu, add_asset
from models import Message, ChatCompletionRequest, ChatCompletionResponse, ChatCompletionResponseChoice
from utils import (
    merge_messages_to_prompt,
    call_original_stream,
    original_to_openai_stream_with_cleanup,
    delete_conversation
)
from mcp_server import mcp_server, server_initialized
from logger import logger

# 初始化数据库
init_db()

# 模板配置
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")

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

    logger.error(f"请求验证错误: {exc.errors()}, body: {body_str}")

    return JSONResponse(
        status_code=422,
        content={
            "detail": exc.errors(),
            "body": body_str
        }
    )

# ---------- 发送短信验证码 ----------
@app.post("/send-sms")
async def send_sms_endpoint(phone: str = Form(...)):
    return await send_sms(phone)

# ---------- 登录页面 ----------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login_post(phone: str = Form(...), code: str = Form(...), invite_code: str = Form("")):
    return await login(phone, code, invite_code)

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
        logger.error(f"图片上传未知错误: {e}")
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
            error_text = await resp.aread()
            logger.error(f"获取模型列表失败: {resp.status_code} {error_text.decode()}")
            raise HTTPException(status_code=resp.status_code, detail=error_text.decode())
        data = resp.json()
        if data.get("code") != 0:
            logger.error(f"获取模型列表业务错误: {data.get('msg')}")
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
    logger.info(f"请求 model={request.model}, stream={request.stream}, file_ids={request.file_ids}")
    token = get_latest_token()
    if not token:
        raise HTTPException(status_code=401, detail="No token, please login first")
    if not await validate_token(token):
        delete_token(token)
        raise HTTPException(status_code=401, detail="Token expired or invalid")

    file_ids = request.file_ids
    if file_ids is not None:
        if not isinstance(file_ids, list):
            logger.warning(f"file_ids 类型错误: {type(file_ids)}，将设为空列表")
            file_ids = []
        else:
            file_ids = [fid for fid in file_ids if isinstance(fid, str)]

    messages_dict = [msg.model_dump() for msg in request.messages]
    prompt = merge_messages_to_prompt(messages_dict)

    try:
        original_gen = call_original_stream(prompt, request.model, token, file_ids)
    except Exception as e:
        logger.error(f"调用原始流失败: {e}")
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
async def test_page(request: Request):
    return templates.TemplateResponse("test.html", {"request": request})

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
            await asyncio.sleep(0.1)
            server_initialized = True
            logger.info("MCP 服务器初始化完成")
            await task
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            server_initialized = False

async def mcp_messages_app(scope, receive, send):
    await sse_transport.handle_post_message(scope, receive, send)

app.mount("/mcp/messages", mcp_messages_app)

if __name__ == "__main__":
    logger.info(f"启动服务，监听 {HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)