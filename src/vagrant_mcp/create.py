"""vm_create 로직: 템플릿 폴더를 복사해 새 Vagrant VM 을 만든다.

- vm_create_options : 호스트별 기본값(템플릿 Vagrantfile), 자원 상한, 남은 IP, 이미지 목록을 보여준다.
- vm_create         : 값 검증 -> IP 확정 -> {workdir}[/{project}]/{name} 폴더 생성 -> `vagrant up` 을 끝날 때까지 기다린다.
                      만든 폴더에는 .vagrant-mcp 표시 파일을 남긴다 (vm_list 의 managed 판단에 쓰인다).
                      vagrant up 출력은 파일로 남기지 않고 마지막 부분만 결과(output_tail)에 담는다.
- IP        : 모든 호스트가 같은 브리지 망을 쓰므로 "모든 호스트" Vagrantfile 의 IP 를 사용 중으로 본다.
- 락(느슨함): 서버 안에서는 _ALLOC_LOCK 으로 IP 고르기 ~ 파일 쓰기 구간만 막는다.
              vagrant up 은 락을 푼 뒤 별도 SSH 호출로 돌려서, 그동안 다른 생성 요청을 막지 않는다.
              호스트에서는 workdir/.vagrant-mcp.lock 디렉터리로 막고, 10분 넘게 남은 락은 치운다.
              락 안에서 Vagrantfile grep + ping 으로 IP 를 한 번 더 확인한다.
"""

import base64
import json
import posixpath
import re
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from pydantic import BaseModel, computed_field

from .config import HostConfig, find_host, get_settings, to_gi
from .hosts import HostInfo, query_host
from .ssh import run_command
from .vms import MARKER_FILE, OWNER_FILE, OWNER_RE, HostVms, list_vms, parse_vagrantfile, scan_root_of

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
# 프로젝트 폴더는 기존 폴더 이름(k8s-test, redis_cluster 등)을 그대로 쓸 수 있게 조금 느슨하게
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RESERVED = {"templates"}
_IP_PLACEHOLDER = "__MCP_IP__"
_MAX_IP_TRIES = 10
_EXIT_MARK = "@@VAGRANT_UP_EXIT="
_UP_TAIL_LINES = 60
_PROGRESS_RE = re.compile(r"\(Reading database \.\.\. \d+%\s*$")


class ImageSpec(BaseModel):
    os: str
    version: str  # 고정 버전 (사용자가 고르지 않음)
    password_script: str  # 템플릿 폴더에 있어야 하는 OS 별 SSH 비밀번호 스크립트


IMAGES: dict[str, ImageSpec] = {
    "ubuntu/jammy64": ImageSpec(os="Ubuntu 22.04 LTS", version="20241002.0.0", password_script="ssh_password_yes.sh"),
    "bento/rockylinux-8": ImageSpec(os="Rocky Linux 8", version="202510.26.0", password_script="ssh_password_centos.sh"),
    "bento/rockylinux-9": ImageSpec(os="Rocky Linux 9", version="202510.26.0", password_script="ssh_password_centos.sh"),
}

# 고를 수 있는 메모리(MB). 기본값은 템플릿 값과 상관없이 DEFAULT_MEMORY_MB
MEMORY_CHOICES_MB = (512, 1024, 2048, 4096, 8192, 16384, 32768)
DEFAULT_MEMORY_MB = 16384


def memory_label(mb: int) -> str:
    size = f"{mb // 1024}Gi" if mb >= 1024 else f"{mb}Mi"
    return f"{mb} MB ({size})" + (" ★ 기본값" if mb == DEFAULT_MEMORY_MB else "")


_ALLOC_LOCK = threading.Lock()

_READ_TEMPLATE = r"""
[ -f __TPL__/Vagrantfile ] || exit 5
cat __TPL__/Vagrantfile
"""

_CREATE_SCRIPT = r"""
WD=__WD__; DIR=__DIR__; NAME=__NAME__; TPL=__TPL__; SCAN=__SCAN__
LOCK="$WD/.vagrant-mcp.lock"
command -v vagrant >/dev/null 2>&1 || exit 14
[ -d "$TPL" ] || exit 11
[ -f "$TPL"/__PWSCRIPT__ ] || exit 16
if ! mkdir "$LOCK" 2>/dev/null; then
  # 10분 넘게 남은 락은 비정상 종료로 보고 치운다
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
    rmdir "$LOCK" 2>/dev/null; mkdir "$LOCK" 2>/dev/null || exit 10
  else
    exit 10
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT
[ -e "$DIR/$NAME" ] && exit 12
IP=""
for c in __CANDIDATES__; do
  re="ip:[[:space:]]*[\"']$(echo "$c" | sed 's/\./\\./g')[\"']"
  grep -rqsE --include=Vagrantfile --exclude-dir=.vagrant --exclude-dir=templates \
    --exclude-dir=.git --exclude-dir=node_modules "$re" "$SCAN" && continue
  ping -c1 -W1 "$c" >/dev/null 2>&1 && continue
  IP="$c"; break
done
[ -n "$IP" ] || exit 13
mkdir -p "$DIR" && mkdir "$DIR/$NAME" && cp -r "$TPL"/. "$DIR/$NAME"/ || exit 15
rm -rf "$DIR/$NAME/.vagrant" "$DIR/$NAME/__OWNER_FILE__"
echo __VF_B64__ | base64 -d | sed "s/__MCP_IP__/$IP/" > "$DIR/$NAME/Vagrantfile" || exit 15
printf 'created_by=vagrant-mcp\ncreated_at=%s\nip=%s\n' "$(date -Is)" "$IP" > "$DIR/$NAME/__MARKER__"
OWNER_B64=__OWNER_B64__
[ -n "$OWNER_B64" ] && { echo "$OWNER_B64" | base64 -d > "$DIR/$NAME/__OWNER_FILE__" || exit 15; }
rmdir "$LOCK"; trap - EXIT
echo "IP=$IP"
"""

# 포그라운드로 끝까지 기다린다. stderr 도 stdout 으로 합쳐서 한 번에 읽는다.
_UP_SCRIPT = r"""
cd __VMDIR__ || exit 17
vagrant up 2>&1 < /dev/null
echo "__EXIT_MARK__$?"
"""

_CREATE_ERRORS = {
    10: "다른 생성 작업이 진행 중입니다 (호스트 락). 잠시 후 다시 시도하세요.",
    11: "템플릿 폴더가 없습니다: {tpl}",
    12: "같은 이름의 폴더가 이미 있습니다: {path}",
    13: "후보 IP 가 모두 사용 중입니다 (Vagrantfile 에 있거나 ping 응답): {cands}",
    14: "호스트에 vagrant 가 설치되어 있지 않습니다.",
    15: "VM 폴더/Vagrantfile 생성 실패: {stderr}",
    16: "템플릿 폴더에 이미지용 스크립트가 없습니다: {tpl}/{pw}",
}


class VmDefaults(BaseModel):
    image: str | None = None
    cpu: int | None = None
    memory_mb: int | None = None
    bridge: str | None = None


class Limits(BaseModel):
    max_memory_mb: int  # VM 하나에 줄 수 있는 최대 메모리
    disk_ok: bool  # 디스크 여유가 최소 기준 이상인지
    cpu_total: int
    vcpu_allocated: int  # 실행 중 VM vCPU 합계 (참고용. CPU 는 제한하지 않는다)
    mem_total_mb: int
    mem_available_mb: int
    mem_allocated_mb: int  # 실행 중 VM 메모리 합계
    disk_available_mb: int
    rule: str

    @computed_field
    def max_memory_gi(self) -> float | None:
        return to_gi(self.max_memory_mb)

    @computed_field
    def mem_total_gi(self) -> float | None:
        return to_gi(self.mem_total_mb)

    @computed_field
    def mem_available_gi(self) -> float | None:
        return to_gi(self.mem_available_mb)

    @computed_field
    def mem_allocated_gi(self) -> float | None:
        return to_gi(self.mem_allocated_mb)

    @computed_field
    def disk_available_gi(self) -> float | None:
        return to_gi(self.disk_available_mb)


class MemoryChoice(BaseModel):
    memory_mb: int
    label: str  # 예: "16384 MB (16Gi) ★ 기본값"
    default: bool
    allowed: bool  # 이 호스트 메모리 상한(limits.max_memory_mb) 이하인지


class HostCreateOptions(BaseModel):
    host: str
    address: str
    reachable: bool
    error: str | None = None
    defaults: VmDefaults | None = None
    limits: Limits | None = None
    ip_ranges: list[str] = []  # redact_ips 면 비움
    available_ips: list[str] = []  # redact_ips 면 비움 (available_ip_count 만 준다)
    available_ip_count: int = 0
    default_project: str | None = None  # project 를 비우면 쓰는 폴더 (null 이면 workdir 바로 아래)
    projects: list[str] = []  # VM 이 들어 있는 프로젝트 폴더 (project 로 고를 수 있음)
    existing: list[str] = []  # 이미 있는 VM 폴더 ("이름" 또는 "프로젝트/이름"). 같은 위치에 같은 이름 불가
    images: dict[str, str] = {}  # 선택 가능한 이미지 -> OS 이름
    memory_choices: list[MemoryChoice] = []  # 고를 수 있는 메모리 (default=true 가 기본값)


class CreateResult(BaseModel):
    host: str
    project: str | None = None
    name: str
    ip: str
    image: str
    cpu: int
    memory_mb: int
    path: str
    # running : vagrant up 성공 (프로비저닝 스크립트까지 완료)
    # failed  : vagrant up 실패 (프로비저닝 스크립트 실패 포함). 폴더는 남아 있다
    # unknown : 결과를 받지 못함 (SSH 끊김/타임아웃). vm_list 로 상태 확인
    status: str
    exit_code: int | None = None
    error: str | None = None
    ssh: str  # 접속 명령
    output_tail: str = ""  # vagrant up 출력의 마지막 부분
    warnings: list[str] = []

    @computed_field
    def memory_gi(self) -> float | None:
        return to_gi(self.memory_mb)


def compute_limits(info: HostInfo) -> Limits:
    s = get_settings()
    vcpu_alloc = info.vcpu_allocated or 0
    mem_alloc = info.mem_allocated_mb or 0
    if s.allow_mem_overcommit:
        mem_budget = info.mem_available_mb - s.host_mem_reserve_mb
        mem_rule = f"메모리(오버커밋 허용 모드): 가용 - 호스트 예비 {to_gi(s.host_mem_reserve_mb)}Gi. "
    else:
        mem_budget = min(info.mem_available_mb, info.mem_total_mb - mem_alloc) - s.host_mem_reserve_mb
        mem_rule = f"메모리: min(가용, 전체 - 실행 중 VM 할당) - 호스트 예비 {to_gi(s.host_mem_reserve_mb)}Gi. "
    return Limits(
        max_memory_mb=max(0, mem_budget),
        disk_ok=info.disk_available_mb >= s.min_disk_free_mb,
        cpu_total=info.cpu_total,
        vcpu_allocated=vcpu_alloc,
        mem_total_mb=info.mem_total_mb,
        mem_available_mb=info.mem_available_mb,
        mem_allocated_mb=mem_alloc,
        disk_available_mb=info.disk_available_mb,
        rule=(
            "CPU: 제한 없음. "
            + mem_rule
            + f"디스크: 여유 {to_gi(s.min_disk_free_mb)}Gi 이상"
        ),
    )


def _sub_once(text: str, pattern: str, value: str) -> str:
    out, n = re.subn(pattern, lambda m: m.group(1) + value, text)
    if n != 1:
        raise RuntimeError(f"템플릿 Vagrantfile 형식이 예상과 다름: {pattern!r} 가 {n}번 매칭")
    return out


def render_vagrantfile(
    template: str,
    name: str,
    ip: str,
    cpu: int,
    memory_mb: int,
    image: str,
    bridge: str | None,
    template_dir: str,
) -> str:
    """템플릿에서 이름/IP/CPU/메모리/이미지(/bridge) 만 바꾼다. 나머지(프로비저닝 순서 등)는 그대로."""
    spec = IMAGES[image]
    out = template
    out = _sub_once(out, r'(vm\.define\s+)["\'][^"\']+["\']', f'"{name}"')
    out = _sub_once(out, r'(vm\.hostname\s*=\s*)["\'][^"\']+["\']', f'"{name}"')
    out = _sub_once(out, r'(\bip:\s*)["\'][^"\']+["\']', f'"{ip}"')
    out = _sub_once(out, r'(\.cpus\s*=\s*)\d+', str(cpu))
    out = _sub_once(out, r'(\.memory\s*=\s*)\d+', str(memory_mb))
    out = _sub_once(out, r'(vm\.box\s*=\s*)["\'][^"\']+["\']', f'"{image}"')
    out = _sub_once(out, r'(vm\.box_version\s*=\s*)["\'][^"\']+["\']', f'"{spec.version}"')
    out = _sub_once(out, r'(path:\s*)["\']ssh_password_[\w-]+\.sh["\']', f'"{spec.password_script}"')
    if bridge:
        out = _sub_once(out, r'(\bbridge:\s*)["\'][^"\']+["\']', f'"{bridge}"')
    # private_key_path 의 ".../templates/<키>" 를 이 호스트의 템플릿 경로로 바꾼다 (호스트마다 경로가 다름)
    out = re.sub(
        r"^.*private_key_path.*$",
        lambda line: re.sub(
            r'["\'][^"\']*/templates/([^"\'/]+)["\']', lambda m: f'"{template_dir}/{m.group(1)}"', line.group(0)
        ),
        out,
        flags=re.M,
    )
    return out


def _read_template(host: HostConfig) -> str:
    result = run_command(host, _READ_TEMPLATE.replace("__TPL__", shlex.quote(host.template_path())))
    if result.exit_code == 5:
        raise RuntimeError(f"템플릿 Vagrantfile 이 없음: {host.template_path()}/Vagrantfile")
    if result.exit_code != 0:
        raise RuntimeError(f"템플릿 읽기 실패 exit={result.exit_code}: {result.stderr.strip()[:200]}")
    return result.stdout


def _defaults(host: HostConfig, template: str) -> VmDefaults:
    v = parse_vagrantfile(template)
    return VmDefaults(image=v["box"], cpu=v["cpu"], memory_mb=DEFAULT_MEMORY_MB, bridge=host.bridge or v["bridge"])


def _used_ips(all_vms: list[HostVms]) -> tuple[set[str], list[str]]:
    used = {h.address for h in get_settings().hosts}
    warnings: list[str] = []
    for hv in all_vms:
        if not hv.reachable:
            warnings.append(f"{hv.host} 조회 실패 -> 그 호스트 VM 의 IP 는 확인 못함 ({hv.error})")
        used.update(vm.ip for vm in hv.vms if vm.ip)
    return used, warnings


def _rel(host: HostConfig, path: str) -> str:
    base = host.workdir.rstrip("/") + "/"
    return path[len(base):] if path.startswith(base) else path


def _existing(host: HostConfig, all_vms: list[HostVms]) -> list[str]:
    return sorted(_rel(host, vm.path) for hv in all_vms if hv.host == host.name for vm in hv.vms)


def _projects(host: HostConfig, all_vms: list[HostVms]) -> list[str]:
    return sorted({vm.project for hv in all_vms if hv.host == host.name for vm in hv.vms if vm.project})


def _vm_dir(host: HostConfig, project: str | None, name: str) -> str:
    base = host.workdir.rstrip("/")
    return f"{base}/{project}/{name}" if project else f"{base}/{name}"


def _check_project(project: str | None) -> None:
    if project is not None and (not _PROJECT_RE.fullmatch(project) or project in _RESERVED):
        raise ValueError("프로젝트 폴더 이름은 영문/숫자/._- 만 가능하고 한 단계만 지정할 수 있습니다 (예: k8s-test)")


def _host_options(host: HostConfig, info: HostInfo, all_vms: list[HostVms], used: set[str]) -> HostCreateOptions:
    hide_ips = get_settings().redact_ips
    out = HostCreateOptions(
        host=host.name,
        address=host.address,
        reachable=False,
        ip_ranges=[] if hide_ips else host.ip_ranges,
        default_project=host.default_project,
    )
    if not info.reachable:
        out.error = info.error
        return out
    try:
        out.defaults = _defaults(host, _read_template(host))
    except Exception as e:
        out.error = f"{type(e).__name__}: {e}"
    out.reachable = True
    out.limits = compute_limits(info)
    free_ips = [ip for ip in host.ip_pool() if ip not in used]
    out.available_ip_count = len(free_ips)
    out.available_ips = [] if hide_ips else free_ips
    out.projects = _projects(host, all_vms)
    out.existing = _existing(host, all_vms)
    out.images = {k: v.os for k, v in IMAGES.items()}
    out.memory_choices = [
        MemoryChoice(
            memory_mb=m,
            label=memory_label(m),
            default=m == DEFAULT_MEMORY_MB,
            allowed=m <= out.limits.max_memory_mb,
        )
        for m in MEMORY_CHOICES_MB
    ]
    return out


def create_options(host: str | None = None) -> list[HostCreateOptions]:
    targets = get_settings().hosts if host is None else [find_host(host)]
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=len(targets) + 1) as pool:
        f_vms = pool.submit(list_vms)  # IP 는 모든 호스트 기준
        infos = list(pool.map(query_host, targets))
        all_vms = f_vms.result()
    used, _ = _used_ips(all_vms)
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        return list(pool.map(lambda pair: _host_options(*pair, all_vms, used), zip(targets, infos)))


def create_vm(
    host: str,
    name: str,
    ip: str | None = None,
    cpu: int | None = None,
    memory_mb: int | None = None,
    image: str | None = None,
    project: str | None = None,
    owner: str | None = None,
) -> CreateResult:
    h = find_host(host)
    if not _NAME_RE.fullmatch(name) or name in _RESERVED:
        raise ValueError("이름은 소문자/숫자/하이픈, 1~32자, 하이픈으로 시작·끝 불가 (예: web-01)")
    project = project or h.default_project
    _check_project(project)
    owner = owner.strip() if owner else None
    if owner is not None and not OWNER_RE.fullmatch(owner):
        raise ValueError("owner 는 1~64자, 줄바꿈/제어문자 불가")
    owner_json = (
        json.dumps({"owner": owner, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        if owner
        else ""
    )
    if image is not None and image not in IMAGES:
        raise ValueError(f"지원하지 않는 이미지: {image} (가능: {', '.join(IMAGES)})")
    if memory_mb is not None and memory_mb not in MEMORY_CHOICES_MB:
        raise ValueError(f"memory_mb 는 {', '.join(map(str, MEMORY_CHOICES_MB))} 중 하나여야 합니다.")

    with _ALLOC_LOCK:
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_tpl = pool.submit(_read_template, h)
            f_info = pool.submit(query_host, h)
            f_vms = pool.submit(list_vms)
            template, info, all_vms = f_tpl.result(), f_info.result(), f_vms.result()
        if not info.reachable:
            raise RuntimeError(f"{h.name} 접속/조회 실패: {info.error}")

        defaults = _defaults(h, template)
        cpu = cpu if cpu is not None else defaults.cpu
        memory_mb = memory_mb if memory_mb is not None else DEFAULT_MEMORY_MB
        image = image or defaults.image
        if cpu is None:
            raise RuntimeError("템플릿에서 cpus 기본값을 찾지 못함. cpu 를 직접 지정하세요.")
        if image not in IMAGES:
            raise RuntimeError(f"템플릿의 box({image}) 가 지원 이미지 목록에 없음. image 를 직접 지정하세요.")

        limits = compute_limits(info)
        if cpu < 1:
            raise ValueError("cpu 는 1 이상이어야 합니다.")
        if memory_mb > limits.max_memory_mb:
            allowed = [str(m) for m in MEMORY_CHOICES_MB if m <= limits.max_memory_mb]
            raise ValueError(
                f"memory_mb={memory_mb} 는 {h.name} 의 상한({limits.max_memory_mb}MB)을 넘습니다. "
                f"가능: {', '.join(allowed) or '없음'}. {limits.rule}"
            )
        if not limits.disk_ok:
            raise ValueError(f"{h.name} 디스크 여유가 부족합니다 ({limits.disk_available_mb}MB). {limits.rule}")

        rel = f"{project}/{name}" if project else name
        if rel in _existing(h, all_vms):
            raise ValueError(f"{h.name} 에 같은 위치의 VM 이 이미 있습니다: {rel}")

        used, warnings = _used_ips(all_vms)
        pool_ips = h.ip_pool()
        if not pool_ips:
            raise RuntimeError(f"{h.name} 에 ip_ranges 설정이 없습니다.")
        if ip is not None:
            if ip not in pool_ips:
                raise ValueError(f"{ip} 는 {h.name} 의 허용 대역({', '.join(h.ip_ranges)}) 밖입니다.")
            if ip in used:
                raise ValueError(f"{ip} 는 이미 사용 중입니다.")
            candidates = [ip]
        else:
            candidates = [x for x in pool_ips if x not in used][:_MAX_IP_TRIES]
            if not candidates:
                raise RuntimeError(f"{h.name} 의 허용 대역에 남은 IP 가 없습니다.")

        tpl_dir = h.template_path()
        vagrantfile = render_vagrantfile(
            template, name, _IP_PLACEHOLDER, cpu, memory_mb, image, h.bridge, tpl_dir
        )
        pw_script = IMAGES[image].password_script
        path = _vm_dir(h, project, name)
        script = (
            _CREATE_SCRIPT.replace("__WD__", shlex.quote(h.workdir.rstrip("/")))
            .replace("__DIR__", shlex.quote(posixpath.dirname(path)))
            .replace("__MARKER__", MARKER_FILE)
            .replace("__OWNER_FILE__", OWNER_FILE)
            .replace("__OWNER_B64__", shlex.quote(base64.b64encode(owner_json.encode()).decode()))
            .replace("__NAME__", shlex.quote(name))
            .replace("__TPL__", shlex.quote(tpl_dir))
            .replace("__SCAN__", shlex.quote(scan_root_of(h)))
            .replace("__PWSCRIPT__", shlex.quote(pw_script))
            .replace("__CANDIDATES__", " ".join(shlex.quote(c) for c in candidates))
            .replace("__VF_B64__", base64.b64encode(vagrantfile.encode()).decode())
        )
        result = run_command(h, script, timeout=120)

    if result.exit_code in _CREATE_ERRORS:
        raise RuntimeError(
            _CREATE_ERRORS[result.exit_code].format(
                tpl=tpl_dir,
                path=path,
                pw=pw_script,
                cands=", ".join(candidates),
                stderr=result.stderr.strip()[:200],
            )
        )
    m = re.search(r"^IP=(\S+)$", result.stdout, re.M)
    if result.exit_code != 0 or not m:
        raise RuntimeError(f"생성 실패 exit={result.exit_code}: {result.stderr.strip()[:200]}")

    status, exit_code, tail, error = _vagrant_up(h, path)
    return CreateResult(
        host=h.name,
        project=project,
        name=name,
        ip=m.group(1),
        image=image,
        cpu=cpu,
        memory_mb=memory_mb,
        path=path,
        status=status,
        exit_code=exit_code,
        error=error,
        ssh=f"{h.name} 에서: cd {path} && vagrant ssh" if get_settings().redact_ips else f"ssh vagrant@{m.group(1)}",
        output_tail=tail,
        warnings=warnings,
    )


def _vagrant_up(h: HostConfig, path: str) -> tuple[str, int | None, str, str | None]:
    """vagrant up 을 끝날 때까지 돌리고 (status, exit_code, 출력 마지막 부분, 에러) 를 돌려준다."""
    script = _UP_SCRIPT.replace("__VMDIR__", shlex.quote(path)).replace("__EXIT_MARK__", _EXIT_MARK)
    try:
        result = run_command(h, script, timeout=get_settings().vm_create_timeout_sec)
    except Exception as e:  # SSH 끊김, 타임아웃 등. 호스트에서는 계속 돌고 있을 수 있다
        return "unknown", None, "", f"vagrant up 결과를 받지 못함 ({type(e).__name__}: {e}). vm_list 로 상태를 확인하세요."
    if result.exit_code == 17:
        return "failed", None, "", f"VM 폴더로 이동하지 못함: {path}"

    # \r 로 덮어쓰는 진행률은 마지막 상태만, apt 의 "(Reading database ... N%" 줄은 빼서 tail 에 쓸모 있는 줄을 남긴다
    lines = [l.rsplit("\r", 1)[-1] for l in result.stdout.rstrip("\n").splitlines()]
    lines = [l for l in lines if not _PROGRESS_RE.search(l)]
    m = re.fullmatch(re.escape(_EXIT_MARK) + r"(\d+)", lines[-1].strip()) if lines else None
    if not m:
        tail = "\n".join(lines[-_UP_TAIL_LINES:])
        return "unknown", None, tail, "vagrant up 종료 코드를 받지 못함. vm_list 로 상태를 확인하세요."
    code = int(m.group(1))
    tail = "\n".join(lines[:-1][-_UP_TAIL_LINES:])
    if code == 0:
        return "running", 0, tail, None
    return "failed", code, tail, "vagrant up 실패 (프로비저닝 스크립트 실패 포함). output_tail 을 확인하세요. 폴더는 남아 있습니다."
