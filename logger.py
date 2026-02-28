import logging
import sys
from logging.handlers import RotatingFileHandler
from config import LOG_FILE, LOG_LEVEL

def setup_logger(name="zaiwen-proxy", log_file=LOG_FILE, level=LOG_LEVEL):
    """设置统一的日志记录器"""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logger.level)
    console_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_format)

    # 文件处理器（可选）
    if log_file:
        file_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5)
        file_handler.setLevel(logger.level)
        file_handler.setFormatter(console_format)
        logger.addHandler(file_handler)

    logger.addHandler(console_handler)

    return logger

# 全局日志实例
logger = setup_logger()