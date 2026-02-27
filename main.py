import asyncio
import json
import uuid
from typing import Dict, Any, List

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request as StarletteRequest
from mcp.server.sse import SseServerTransport

from config import HEADERS_TEMPLATE, BASE_URL, PORT, HOST
from database import init_db, get_latest_token, delete_token, save_token
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
import time

# 初始化数据库
init_db()

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
async def send_sms_endpoint(phone: str = Form(...)):
    return await send_sms(phone)

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

    messages_dict = [msg.model_dump() for msg in request.messages]
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
            await asyncio.sleep(0.1)
            server_initialized = True
            print("[MCP] 服务器初始化完成")
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
    uvicorn.run(app, host=HOST, port=PORT)