# mcp_server/core/notify.py
"""
백그라운드 작업(생성/변경/삭제)이 끝나면 웹훅으로 알린다. (예: Open WebUI 채널 수신 웹훅)
VAGRANT_NOTIFY_WEBHOOK 이 없으면 아무것도 하지 않는다. URL 에 토큰이 들어 있으므로 로그/응답에 남기지 않는다.
알림 실패는 작업 결과에 영향을 주지 않는다.
"""
import asyncio
import json
import logging
import os
import urllib.request

from mcp_server.core.redact import redact_text

logger = logging.getLogger(__name__)

_MAX_LEN = 1000

def _post(url: str, content: str) -> None:
    body = json.dumps({"content": content}).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()

async def notify(content: str, *, error: bool = False) -> None:
    """
    content 를 가리고 잘라서 웹훅으로 보낸다. VAGRANT_NOTIFY_ON=error 면 error=True 인 것만 보낸다.
    """
    url = os.getenv("VAGRANT_NOTIFY_WEBHOOK", "").strip()
    if not url:
        return
    if os.getenv("VAGRANT_NOTIFY_ON", "all").lower() == "error" and not error:
        return

    content = redact_text(content)
    if len(content) > _MAX_LEN:
        content = content[:_MAX_LEN] + "..."
    try:
        await asyncio.to_thread(_post, url, content)
    except Exception as e:
        # 예외 메시지에 URL 이 들어갈 수 있어 종류만 남긴다.
        logger.warning(f"작업 알림 전송 실패: {type(e).__name__}")
