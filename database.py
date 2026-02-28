import sqlite3
import time
from typing import Optional
from config import DB_PATH
from logger import logger

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS tokens
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT, created_at INTEGER)''')
    conn.commit()
    conn.close()
    logger.info("数据库初始化完成")

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
    logger.info(f"新token已保存: {token[:20]}...")

def delete_token(token: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM tokens WHERE token=?", (token,))
    conn.commit()
    conn.close()
    logger.info(f"token已删除: {token[:20]}...")