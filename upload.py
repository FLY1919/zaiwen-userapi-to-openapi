import httpx
from fastapi import HTTPException
from typing import Dict, Any
from config import HEADERS_TEMPLATE, BASE_URL

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