# mcp_server/utils.py
import logging
import os

logger = logging.getLogger(__name__)
        
def get_env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except ValueError:
        logger.warning(f"환경변수 {key}의 타입이 Integer가 아닙니다. 기본값 {default}를 사용합니다.")
        return default
