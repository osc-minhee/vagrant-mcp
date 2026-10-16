# mcp_server/core/redact.py
"""
Client(LLM)로 나가는 오류 메시지 등에서 내부 정보를 가린다. 로그와 DB 에는 원문이 남고, 응답에만 쓴다.

가리는 것
- 비밀값   : password/token/secret/api key 뒤의 값, PRIVATE KEY 블록
- 호스트 주소: 설정한 호스트의 접속 주소(IP/도메인) -> <host-addr:호스트이름>, 그 앞의 계정(user@) -> ***@
- 경로     : 설정한 호스트의 workdir -> <workdir:호스트이름>, 그 밖의 /home/<계정>, /Users/<계정> -> /home/***
- 계정명   : 설정한 호스트의 SSH 계정 (vagrant, root 처럼 흔한 이름은 제외)
가리지 않는 것: VM 의 IP (사용자에게 알려 줘야 한다), 호스트 이름, 프로젝트/VM 폴더 이름
"""
import re
from typing import Dict, Set

# 설정에서 읽은 값 -> 응답에 대신 쓸 표시. load_hosts 가 register_host 로 채운다.
_ADDRESSES: Dict[str, str] = {}
_WORKDIRS: Dict[str, str] = {}
_USERS: Set[str] = set()

# 단어 하나로 흔히 쓰여서 가리면 문장이 망가지는 계정명은 제외한다.
_COMMON_USERS = {"vagrant", "root", "user", "admin", "ubuntu"}

_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|passphrase|token|secret|api[_-]?key|access[_-]?key)(['\"]?\s*[:=]\s*['\"]?)([^\s,;'\"}]+)"
)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
_HOME_RE = re.compile(r"(?<![\w.-])/(home|Users)/[^/\s'\":;,)\]]+")
_USER_AT_HOST_RE = re.compile(r"[\w.-]+@(?=<host-addr:)")

def reset() -> None:
    _ADDRESSES.clear()
    _WORKDIRS.clear()
    _USERS.clear()

def register_host(name: str, address: str, workdir: str, user: str) -> None:
    """
    호스트 한 대의 접속 주소, workdir, SSH 계정을 가릴 대상으로 등록한다.
    """
    address = (address or "").strip().lower()
    if address and address != "local":
        _ADDRESSES[address] = f"<host-addr:{name}>"

    workdir = (workdir or "").rstrip("/")
    if len(workdir) > 1:
        _WORKDIRS[workdir] = f"<workdir:{name}>"

    user = (user or "").strip()
    if len(user) >= 3 and user.lower() not in _COMMON_USERS:
        _USERS.add(user)

def _replace_words(text: str, mapping: Dict[str, str], *, ignore_case: bool) -> str:
    # 긴 것부터 바꾼다. 192.168.50.1 이 192.168.50.100 의 앞부분으로 잡히지 않도록 앞뒤 경계를 본다.
    for value in sorted(mapping, key=len, reverse=True):
        pattern = rf"(?<![\w-]){re.escape(value)}(?![\w-]|\.\w)"
        text = re.sub(pattern, lambda _m, v=value: mapping[v], text, flags=re.IGNORECASE if ignore_case else 0)
    return text

def redact_text(text: str) -> str:
    if not text:
        return text

    text = _PRIVATE_KEY_RE.sub("<PRIVATE KEY 숨김>", text)
    text = _SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + "***", text)

    text = _replace_words(text, _ADDRESSES, ignore_case=True)
    text = _USER_AT_HOST_RE.sub("***@", text)

    for workdir in sorted(_WORKDIRS, key=len, reverse=True):
        text = re.sub(rf"(?<![\w.-]){re.escape(workdir)}(?![\w-])", lambda _m, w=workdir: _WORKDIRS[w], text)
    text = _HOME_RE.sub(lambda m: f"/{m.group(1)}/***", text)

    if _USERS:
        text = _replace_words(text, {u: "***" for u in _USERS}, ignore_case=False)
    return text
