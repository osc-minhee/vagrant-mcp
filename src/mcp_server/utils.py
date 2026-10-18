# mcp_server/utils.py
import ipaddress
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)

def now_local() -> str:
    """
    지금 시각 (서버 시간대, 예: 2026-10-18T15:58:58+09:00). 조회 응답에 넣어 언제 조회한 결과인지 알 수 있게 한다.
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")

def utc_to_local(value: Optional[str]) -> Optional[str]:
    """
    SQLite CURRENT_TIMESTAMP(UTC, 시간대 표시 없음)를 서버 시간대로 바꾼다. 그대로 보내면 한국 시간보다 9시간 전처럼 보인다.
    """
    try:
        dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return value
    return dt.astimezone().isoformat(timespec="seconds")

def get_env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except ValueError:
        logger.warning(f"환경변수 {key}의 타입이 Integer가 아닙니다. 기본값 {default}를 사용합니다.")
        return default

def expand_ip_range(spec: str) -> List[str]:
    """
    "10.0.0.60-89" / "10.0.0.60-10.0.0.89" / "10.0.0.60" 을 IP 목록으로 푼다. 여러 대역은 쉼표로 구분한다.
    """
    ips: List[str] = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        start, _, end = part.partition("-")
        first = ipaddress.IPv4Address(start.strip())
        end = end.strip()
        if not end:
            last = first
        elif "." in end:
            last = ipaddress.IPv4Address(end)
        else:
            last = ipaddress.IPv4Address(f"{start.strip().rsplit('.', 1)[0]}.{end}")
        if last < first:
            raise ValueError(f"IP 대역의 끝이 시작보다 작습니다: {part}")
        ips.extend(str(ipaddress.IPv4Address(i)) for i in range(int(first), int(last) + 1))
    return list(dict.fromkeys(ips))
