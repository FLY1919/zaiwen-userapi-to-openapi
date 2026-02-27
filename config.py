import os
from dotenv import load_dotenv

load_dotenv()

# 基础配置
BASE_URL = os.getenv("BASE_URL", "https://back.zaiwenai.com")
CHANNEL = os.getenv("CHANNEL", "web.zaiwenai.com")
ORIGIN = os.getenv("ORIGIN", "https://www.zaiwenai.com")
REFERER = os.getenv("REFERER", "https://www.zaiwenai.com/")
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
DB_PATH = os.getenv("DB_PATH", "proxy.db")

# 模型配置
DEFAULT_IMAGE_MODEL = os.getenv("DEFAULT_IMAGE_MODEL", "Flux-2-Pro")
DEFAULT_MUSIC_MODEL = os.getenv("DEFAULT_MUSIC_MODEL", "zaiwen")

# 任务轮询配置
TASK_TIMEOUT_SECONDS = int(os.getenv("TASK_TIMEOUT_SECONDS", "1200"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "2.0"))
POLL_MAX_ATTEMPTS = int(TASK_TIMEOUT_SECONDS / POLL_INTERVAL)
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "200"))

# 服务配置
PORT = int(os.getenv("PORT", "8001"))
HOST = os.getenv("HOST", "::")

# 请求头模板
HEADERS_TEMPLATE = {
    "channel": CHANNEL,
    "Origin": ORIGIN,
    "Referer": REFERER,
    "User-Agent": USER_AGENT
}