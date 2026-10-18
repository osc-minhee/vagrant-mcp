# mcp_server/core/hosts.py
import ipaddress
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from mcp_server.core import redact
from mcp_server.core.constants import PROJECT_PATTERN, SECRETS_DIR
from mcp_server.core.executor.runner import CommandRunner, build_runner
from mcp_server.utils import expand_ip_range, get_env_int

logger = logging.getLogger(__name__)

# 단일 호스트 설정(VAGRANT_HOST 등)을 쓸 때 붙는 이름
DEFAULT_HOST_NAME = "default"
DEFAULT_WORKDIR = "~/vagrant-mcp"

@dataclass
class VagrantHost:
    """
    Vagrant 가 설치된 호스트 한 대의 접속 정보와 할당 설정.
    """
    name: str
    runner: CommandRunner
    workdir: str
    ip_pool: List[str] = field(default_factory=list)
    bridge: Optional[str] = None
    # vg_create_vm 에서 project 를 비웠을 때 쓰는 프로젝트 폴더. 없으면 workdir 바로 아래에 만든다.
    default_project: Optional[str] = None
    # IP 를 고를 때 이 폴더 아래의 Vagrantfile 에 적힌 IP 를 사용 중으로 본다. (기본: workdir)
    scan_root: str = ""
    # VM 의 기본 경로로 쓸 bridge 네트워크의 게이트웨이. 없으면 Vagrant 기본값(NAT)이라 다른 대역(VPN 등)에서 접속되지 않는다.
    gateway: Optional[str] = None

def _ip_pool(entry: Dict[str, Any]) -> List[str]:
    specs = entry.get("ip_ranges") or entry.get("ip_range") or []
    if isinstance(specs, str):
        specs = [specs]
    return expand_ip_range(",".join(str(s) for s in specs))

def _register_key_path(path: Optional[str], label: str) -> None:
    # 설정에 적힌 형태('~/' 포함)와 실제 경로를 모두 가린다. SECRETS_DIR 아래면 <secrets> 로도 가려진다.
    if path:
        redact.register_path(path, label)
        redact.register_path(os.path.expanduser(path), label)

def _entries_from_env() -> List[Dict[str, Any]]:
    """
    VAGRANT_HOSTS(JSON 배열)가 있으면 그것을, 없으면 기존 단일 호스트 환경변수(VAGRANT_HOST 등)를 쓴다.
    """
    raw = os.getenv("VAGRANT_HOSTS", "").strip()
    if not raw:
        return [{
            "name": DEFAULT_HOST_NAME,
            "address": os.getenv("VAGRANT_HOST", "local"),
            "user": os.getenv("VAGRANT_SSH_USER", "vagrant"),
            "port": get_env_int("VAGRANT_SSH_PORT", 22),
            "workdir": os.getenv("VAGRANT_WORKDIR", DEFAULT_WORKDIR),
            "ip_range": os.getenv("VAGRANT_IP_RANGE", ""),
            "bridge": os.getenv("VAGRANT_BRIDGE") or None,
            "scan_root": os.getenv("VAGRANT_SCAN_ROOT") or None,
            "gateway": os.getenv("VAGRANT_GATEWAY") or None,
        }]

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"VAGRANT_HOSTS 가 올바른 JSON 이 아닙니다 (값은 한 줄이어야 합니다): {e}") from e
    if not isinstance(entries, list) or not entries:
        raise ValueError("VAGRANT_HOSTS 는 비어 있지 않은 JSON 배열이어야 합니다.")
    return entries

def load_hosts() -> Dict[str, VagrantHost]:
    """
    호스트 설정을 읽어 {이름(소문자): VagrantHost} 로 돌려준다. 입력 순서가 유지되며 첫 번째가 기본 호스트다.
    설정이 잘못되었으면 서버를 시작할 때 바로 알 수 있도록 ValueError 를 낸다.
    """
    hosts: Dict[str, VagrantHost] = {}
    redact.reset()
    # 키 파일 등 비밀 파일 위치가 오류 메시지(예: Permission denied: '/app/secrets/.ssh/host_key')로 나가지 않게 한다.
    redact.register_path(SECRETS_DIR.as_posix(), "<secrets>")
    redact.register_path(str(SECRETS_DIR), "<secrets>")
    _register_key_path(os.getenv("VAGRANT_SSH_KEY"), "<ssh-key>")

    for i, entry in enumerate(_entries_from_env()):
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("address"):
            raise ValueError(f"VAGRANT_HOSTS[{i}] 에는 name 과 address 가 필요합니다.")

        name = str(entry["name"]).strip()
        if name.lower() in hosts:
            raise ValueError(f"VAGRANT_HOSTS 에 같은 이름의 호스트가 둘 이상 있습니다: {name}")

        try:
            ip_pool = _ip_pool(entry)
            port = int(entry.get("port", 22))
        except ValueError as e:
            raise ValueError(f"VAGRANT_HOSTS 의 '{name}' 설정이 올바르지 않습니다: {e}") from e

        # 호스트별 default_project 가 없으면 전체 기본값(VAGRANT_DEFAULT_PROJECT)을 쓴다.
        default_project = str(entry.get("default_project") or os.getenv("VAGRANT_DEFAULT_PROJECT") or "").strip() or None
        if default_project and not re.fullmatch(PROJECT_PATTERN, default_project):
            raise ValueError(
                f"VAGRANT_HOSTS 의 '{name}' 의 default_project 가 올바르지 않습니다: {default_project!r} "
                "(영문/숫자/._- 만, 한 단계 폴더 이름)"
            )

        workdir = (entry.get("workdir") or DEFAULT_WORKDIR).rstrip("/") or "/"
        scan_root = (entry.get("scan_root") or workdir).rstrip("/") or "/"

        # 호스트별 gateway 가 없으면 전체 기본값(VAGRANT_GATEWAY)을 쓴다. 둘 다 없으면 기본 경로를 바꾸지 않는다.
        gateway = str(entry.get("gateway") or os.getenv("VAGRANT_GATEWAY") or "").strip() or None
        if gateway:
            try:
                gateway = str(ipaddress.IPv4Address(gateway))
            except ValueError as e:
                raise ValueError(f"VAGRANT_HOSTS 의 '{name}' 의 gateway 가 올바른 IPv4 주소가 아닙니다: {gateway!r}") from e

        hosts[name.lower()] = VagrantHost(
            name=name,
            runner=build_runner(
                address=str(entry["address"]),
                user=entry.get("user") or "vagrant",
                port=port,
                key_path=entry.get("ssh_key") or None,
                password=entry.get("ssh_password") or None,
            ),
            workdir=workdir,
            ip_pool=ip_pool,
            bridge=entry.get("bridge") or None,
            default_project=default_project,
            scan_root=scan_root,
            gateway=gateway,
        )

        # 응답(오류 메시지 등)에서 이 호스트의 주소, 경로, 계정명을 가리기 위해 등록한다.
        redact.register_host(name, str(entry["address"]), workdir, entry.get("user") or "vagrant", scan_root=scan_root)
        _register_key_path(entry.get("ssh_key"), f"<ssh-key:{name}>")

    logger.info(f"Vagrant 호스트 {len(hosts)}대 로드: {', '.join(h.name for h in hosts.values())}")
    return hosts
