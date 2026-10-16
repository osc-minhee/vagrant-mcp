import ipaddress
from functools import lru_cache

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def expand_ip_range(spec: str) -> list[str]:
    """"10.0.0.60-89" / "10.0.0.60-10.0.0.89" / "10.0.0.60" 을 IP 목록으로 푼다."""
    start, _, end = spec.partition("-")
    first = ipaddress.IPv4Address(start.strip())
    end = end.strip()
    if not end:
        last = first
    elif "." in end:
        last = ipaddress.IPv4Address(end)
    else:
        last = ipaddress.IPv4Address(f"{start.strip().rsplit('.', 1)[0]}.{end}")
    if last < first:
        raise ValueError(f"IP 대역의 끝이 시작보다 작음: {spec}")
    return [str(ipaddress.IPv4Address(i)) for i in range(int(first), int(last) + 1)]


class HostConfig(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9._-]{1,32}$")
    address: str
    user: str
    port: int = 22
    # VM 을 만드는 기준 폴더 (그 아래 {name} 또는 {project}/{name} 으로 만든다)
    workdir: str
    scan_root: str | None = None  # VM 을 찾는 범위 (기본: workdir)
    # 새 VM 을 만들 때 복사할 템플릿 폴더 (기본: {workdir}/templates)
    template_dir: str | None = None
    # vm_create 에서 project 를 비우면 쓸 프로젝트 폴더 (예: web -> {workdir}/web/{name}).
    # 없으면 workdir 바로 아래. VM 목록/IP 확인/삭제 범위는 그대로 workdir 기준이다
    default_project: str | None = None
    # 템플릿의 bridge 대신 쓸 NIC 이름 (호스트마다 NIC 이름이 다를 때)
    bridge: str | None = None
    # 이 호스트에 새 VM 을 만들 때 줄 수 있는 IP 대역
    ip_ranges: list[str] = []

    @field_validator("ip_ranges")
    @classmethod
    def _check_ranges(cls, v: list[str]) -> list[str]:
        for spec in v:
            expand_ip_range(spec)
        return v

    def template_path(self) -> str:
        return self.template_dir or f"{self.workdir.rstrip('/')}/templates"

    def ip_pool(self) -> list[str]:
        seen: dict[str, None] = {}
        for spec in self.ip_ranges:
            seen.update(dict.fromkeys(expand_ip_range(spec)))
        return list(seen)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAGRANT_MCP_", env_file=".env", extra="ignore")

    hosts: list[HostConfig] = []

    ssh_password: str | None = None
    ssh_key_path: str | None = None
    ssh_timeout_sec: int = 10
    ssh_strict_host_key: bool = False

    # VM 생성 상한
    # - 메모리: 오버커밋 없음. min(MemAvailable, 전체 - 실행 중 VM 할당) 에서 호스트 몫을 남긴다
    # - CPU   : 제한하지 않는다
    host_mem_reserve_mb: int = 2048
    # True 면 "전체 - 실행 중 VM 할당" 조건을 빼고 가용 메모리만 본다 (이미 오버커밋된 호스트에서 테스트할 때만, 임시로)
    allow_mem_overcommit: bool = False
    # 디스크 여유가 이보다 적으면 생성하지 않는다 (VM 디스크는 쓰면서 늘어난다)
    min_disk_free_mb: int = 20480
    # vm_create 가 vagrant up 을 기다리는 시간 (출력이 이 시간 동안 없으면 끊는다). box 를 처음 받으면 오래 걸린다
    vm_create_timeout_sec: int = 1800

    # VM 삭제 전 점검: 이 기간(일) 안의 로그인/파일 수정을 사용 흔적으로 본다
    delete_window_days: int = 7

    # True 면 도구 응답에서 IPv4 와 호스트 address 를 가린다 (LLM 에 사내 IP 를 보내지 않음).
    # 이때 vm_create_options 는 available_ips 대신 available_ip_count 만 주고, IP 는 자동 할당한다
    redact_ips: bool = True

    server_host: str = "0.0.0.0"
    server_port: int = 8000


def to_gi(mb: int | None) -> float | None:
    """MB(정확히는 MiB, /proc/meminfo·df -m·VirtualBox 기준) -> GiB. free -h 의 Gi 와 같은 단위."""
    return None if mb is None else round(mb / 1024, 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def find_host(name: str) -> HostConfig:
    hosts = get_settings().hosts
    for h in hosts:
        if h.name == name:
            return h
    names = ", ".join(h.name for h in hosts)
    raise ValueError(f"알 수 없는 호스트: {name} (사용 가능: {names})")
