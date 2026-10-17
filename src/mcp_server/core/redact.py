# mcp_server/core/redact.py
"""
Client(LLM)로 나가는 오류 메시지 등에서 내부 정보를 가린다. 실행 로그에는 원문이 남는다.
명령 출력을 잘라서 DB 오류 기록(last_error)에 남길 때는 자르기 전에 먼저 가린다. (잘린 비밀값/주소는 가려지지 않으므로)

가리는 것
- 비밀값   : password/token/secret/api key 뒤의 값, PRIVATE KEY 블록
- 호스트 주소: 설정한 호스트의 접속 주소(IP/도메인) -> <host-addr:호스트이름>, 그 앞의 계정(user@) -> ***@
- 경로     : 설정한 호스트의 workdir -> <workdir:호스트이름>, SECRETS_DIR -> <secrets>, SSH 키 파일 -> <ssh-key:호스트이름>,
             그 밖의 /home/<계정>, /Users/<계정> -> /home/***
- 계정명   : 설정한 호스트의 SSH 계정 (vagrant, root 처럼 흔한 이름은 제외)
가리지 않는 것: VM 의 IP (사용자에게 알려 줘야 한다), 호스트 이름, 프로젝트/VM 폴더 이름
"""
import re
from typing import Dict, Set

# 설정에서 읽은 값 -> 응답에 대신 쓸 표시. load_hosts 가 register_host 로 채운다.
_ADDRESSES: Dict[str, str] = {}
# 경로 -> 표시 (workdir, scan_root, SECRETS_DIR, SSH 키 경로). 긴 것부터 바꾼다.
_PATHS: Dict[str, str] = {}
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
    _PATHS.clear()
    _USERS.clear()

def register_host(name: str, address: str, workdir: str, user: str, scan_root: str = "") -> None:
    """
    호스트 한 대의 접속 주소, workdir(와 scan_root), SSH 계정을 가릴 대상으로 등록한다.
    """
    address = (address or "").strip().lower()
    if address and address != "local":
        _ADDRESSES[address] = f"<host-addr:{name}>"

    for path, label in ((scan_root, "scan_root"), (workdir, "workdir")):
        path = (path or "").rstrip("/")
        if len(path) > 1:
            # workdir 와 scan_root 가 같으면 workdir 표시가 이긴다.
            _PATHS[path] = f"<{label}:{name}>"

    user = (user or "").strip()
    if len(user) >= 3 and user.lower() not in _COMMON_USERS:
        _USERS.add(user)

def register_path(path: str, label: str) -> None:
    """
    호스트 설정 밖의 경로(SECRETS_DIR, SSH 키 파일 등)를 가릴 대상으로 등록한다. '~/' 로 시작하면 /home/<계정> 쪽은 _HOME_RE 가 가린다.
    """
    path = str(path or "").strip().rstrip("/")
    if len(path) > 1:
        _PATHS[path] = label

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

    # /home/<계정> 을 가리기 전에 바꿔야 /home/oscka/.ssh/key 같은 등록 경로가 통째로 가려진다.
    for path in sorted(_PATHS, key=len, reverse=True):
        text = re.sub(rf"(?<![\w.-]){re.escape(path)}(?![\w-])", lambda _m, p=path: _PATHS[p], text)
    text = _HOME_RE.sub(lambda m: f"/{m.group(1)}/***", text)

    if _USERS:
        text = _replace_words(text, {u: "***" for u in _USERS}, ignore_case=False)
    return text
