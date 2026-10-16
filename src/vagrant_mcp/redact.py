"""MCP 응답(도구 결과 + 에러 메시지)에서 LLM 에 보내지 않을 값을 가린다.

- 비밀값  : password/token/secret/api key 뒤의 값, PRIVATE KEY 블록, 설정의 ssh_password / ssh_key_path
- 경로    : 호스트 workdir / scan_root / template_dir -> <workdir> 등, /home/<계정> -> /home/***
- IP/주소 : redact_ips=True 면 IPv4 와 호스트 address 를 가린다

서버 안의 계산(IP 중복 확인 등)은 원래 값으로 하고, 도구 경계(server.tool)에서만 가린다.
"""

import re

from pydantic import BaseModel

from .config import get_settings

IP_MASK = "x.x.x.x"

_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\d|\.\d)")
_HOME_RE = re.compile(r"/home/[^/\s'\"]+")
_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|passphrase|token|secret|api[_-]?key|access[_-]?key)(\s*[:=]\s*|\s+)([^\s,;'\"]+)"
)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)


def _known() -> list[tuple[str, str]]:
    """설정에 있는 값 -> 가린 표시. 긴 것부터 바꾼다 (template_dir 가 workdir 안에 있을 수 있음)."""
    s = get_settings()
    pairs: list[tuple[str, str]] = []
    if s.ssh_password and len(s.ssh_password) >= 4:
        pairs.append((s.ssh_password, "***"))
    if s.ssh_key_path:
        pairs.append((s.ssh_key_path, "<ssh_key_path>"))
    for h in s.hosts:
        for value, mask in ((h.template_dir, "<template_dir>"), (h.scan_root, "<scan_root>"), (h.workdir, "<workdir>")):
            value = (value or "").rstrip("/")
            if len(value) > 1:
                pairs.append((value, mask))
        if s.redact_ips:
            pairs.append((h.address, "<host_address>"))
    return sorted(pairs, key=lambda p: len(p[0]), reverse=True)


def redact_text(text: str) -> str:
    text = _PRIVATE_KEY_RE.sub("<PRIVATE KEY 숨김>", text)
    text = _SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + "***", text)
    for value, mask in _known():
        text = text.replace(value, mask)
    text = _HOME_RE.sub("/home/***", text)
    if get_settings().redact_ips:
        text = _IPV4_RE.sub(IP_MASK, text)
    return text


def _walk(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_walk(v) for v in value]
    if isinstance(value, dict):
        return {k: _walk(v) for k, v in value.items()}
    return value


def redact(value):
    """도구 결과(모델 / 모델 목록)를 같은 타입으로 가려서 돌려준다."""
    if isinstance(value, BaseModel):
        return type(value).model_validate(_walk(value.model_dump()))
    if isinstance(value, list):
        return [redact(v) for v in value]
    return _walk(value)
