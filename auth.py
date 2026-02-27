import httpx
from fastapi import HTTPException
from typing import Dict, Any
from config import HEADERS_TEMPLATE, BASE_URL
from database import get_latest_token, delete_token, save_token

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

async def send_sms(phone: str):
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

async def login(phone: str, code: str, invite_code: str = ""):
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