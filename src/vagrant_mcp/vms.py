"""vm_list 로직: 호스트에 있는 Vagrant VM 들을 조회 (읽기 전용).

- 찾는 범위 : scan_root(기본: workdir) 아래 최대 5단계의 Vagrantfile
              ('templates', '.vagrant', '.git', 'node_modules' 폴더는 건너뜀).
              Vagrantfile 이 있는 폴더 = VM 하나.
- 상태      : 폴더의 .vagrant/machines/*/virtualbox/id (VM UUID) 를 VirtualBox 에 직접 조회
              (vagrant global-status 는 마지막 기록 상태라 오래될 수 있어서 쓰지 않는다)
- IP/CPU 등 : Vagrantfile 내용을 정규식으로 추출
- managed   : 폴더에 .vagrant-mcp 표시 파일이 있는 VM = 이 에이전트(vm_create)가 만든 VM
- unmatched_running : VirtualBox 에서 실행 중인데 어떤 폴더와도 연결되지 않는 VM
                      (스캔 범위 밖이거나, Vagrantfile 이 바뀌어 연결이 끊긴 VM)
"""

import re
import shlex
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, computed_field

from .config import HostConfig, find_host, get_settings, to_gi
from .ssh import run_command

_SSH_TIMEOUT_SEC = 120
MARKER_FILE = ".vagrant-mcp"  # vm_create 가 만든 VM 폴더에 남기는 표시 파일
OWNER_FILE = ".owner.json"  # {"owner": "...", "created_at": "..."} - vm_create 의 owner
OWNER_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")  # 1~64자, 줄바꿈/제어문자 불가

_SCRIPT = r"""
cd __ROOT__ || exit 3
command -v VBoxManage >/dev/null 2>&1 && HAS_VBOX=1 || HAS_VBOX=0
RUNNING=""
[ "$HAS_VBOX" = 1 ] && RUNNING=$(VBoxManage list runningvms 2>/dev/null </dev/null)
find . -mindepth 2 -maxdepth 5 \( -name .vagrant -o -name node_modules -o -name .git -o -name templates \) -prune -o -name Vagrantfile -print 2>/dev/null | sort | while IFS= read -r vf; do
  dir="$(cd "$(dirname "$vf")" && pwd)"
  echo "@@VM@@ $dir"
  echo "@@VAGRANTFILE@@"
  cat "$vf" 2>/dev/null
  echo
  echo "@@STATUS@@"
  [ -f "$dir/__MARKER__" ] && echo "MANAGED"
  found=0
  for idf in "$dir"/.vagrant/machines/*/virtualbox/id; do
    [ -f "$idf" ] || continue
    found=1
    m="$(basename "$(dirname "$(dirname "$idf")")")"
    id="$(cat "$idf")"
    echo "ID:$id"
    if [ "$HAS_VBOX" = 0 ]; then
      echo "ERR:VBoxManage 를 찾을 수 없음"
    elif echo "$RUNNING" | grep -q "{$id}"; then
      echo "$m=running"
    else
      st="$(VBoxManage showvminfo "$id" --machinereadable 2>/dev/null </dev/null | grep '^VMState=' | cut -d'"' -f2)"
      echo "$m=${st:-missing}"
    fi
  done
  [ "$found" = 0 ] && echo "not_created"
  echo "@@END@@"
done
echo "@@RUNNING@@"
for id in $(echo "$RUNNING" | sed -n 's/.*{\(.*\)}$/\1/p'); do
  info="$(VBoxManage showvminfo "$id" --machinereadable 2>/dev/null </dev/null)"
  n="$(echo "$info" | grep '^name=' | cut -d'"' -f2)"
  c="$(echo "$info" | grep '^cpus=' | cut -d= -f2)"
  m="$(echo "$info" | grep '^memory=' | cut -d= -f2)"
  echo "$id|$c|$m|$n"
done
echo "@@ENDRUNNING@@"
true
"""


class VmInfo(BaseModel):
    name: str  # VM 폴더 이름
    project: str | None = None  # 상위 프로젝트 폴더 이름 (예: k8s-test, web)
    host: str
    managed: bool  # True 면 이 에이전트(vm_create)가 만든 VM (.vagrant-mcp 표시 파일)
    # running / poweroff / saved / aborted / not_created(만든 적 없음) /
    # missing(.vagrant 기록은 있는데 VirtualBox 에는 그 VM 이 없음)
    status: str | None = None
    vm_name: str | None = None  # Vagrantfile 의 vm.define 이름
    ip: str | None = None
    cpu: int | None = None
    memory_mb: int | None = None
    box: str | None = None
    bridge: str | None = None
    path: str
    error: str | None = None

    @computed_field
    def memory_gi(self) -> float | None:
        return to_gi(self.memory_mb)


class RunningVm(BaseModel):
    uuid: str
    name: str  # VirtualBox 에 등록된 VM 이름 (예: web-01_default_1700000000000_12345)
    cpu: int | None = None
    memory_mb: int | None = None

    @computed_field
    def memory_gi(self) -> float | None:
        return to_gi(self.memory_mb)


class HostVms(BaseModel):
    host: str
    address: str
    reachable: bool
    error: str | None = None
    vms: list[VmInfo] = []
    # 실행 중인데 어느 폴더와도 연결되지 않은 VM. 자원은 쓰고 있으니 반드시 감안해야 한다.
    unmatched_running: list[RunningVm] = []


def scan_root_of(host: HostConfig) -> str:
    return (host.scan_root or host.workdir).rstrip("/") or "/"


def _build_command(root: str) -> str:
    return _SCRIPT.replace("__ROOT__", shlex.quote(root)).replace("__MARKER__", MARKER_FILE)


def parse_vm_output(text: str) -> list[dict]:
    """스크립트 출력을 VM 별 {path, vagrantfile, status} 로 나눈다."""
    vms: list[dict] = []
    cur: dict | None = None
    section: str | None = None
    for line in text.splitlines():
        if line.startswith("@@VM@@ "):
            cur = {"path": line[len("@@VM@@ "):].strip(), "vagrantfile": [], "status": []}
            section = None
        elif line == "@@VAGRANTFILE@@":
            section = "vagrantfile"
        elif line == "@@STATUS@@":
            section = "status"
        elif line == "@@END@@":
            if cur is not None:
                vms.append(cur)
            cur, section = None, None
        elif cur is not None and section:
            cur[section].append(line)
    return vms


def parse_running(text: str) -> list[RunningVm]:
    """@@RUNNING@@ ~ @@ENDRUNNING@@ 사이의 `uuid|cpus|memory|name` 줄들을 읽는다."""
    out: list[RunningVm] = []
    inside = False
    for line in text.splitlines():
        if line == "@@RUNNING@@":
            inside = True
        elif line == "@@ENDRUNNING@@":
            inside = False
        elif inside and line.count("|") >= 3:
            uuid, cpu, mem, name = line.split("|", 3)
            out.append(
                RunningVm(
                    uuid=uuid,
                    name=name,
                    cpu=int(cpu) if cpu.isdigit() else None,
                    memory_mb=int(mem) if mem.isdigit() else None,
                )
            )
    return out


def parse_vagrantfile(content: str) -> dict:
    """Vagrantfile 에서 VM 정보를 뽑는다. 주석(#) 줄은 무시."""
    code = "\n".join(l for l in content.splitlines() if not l.lstrip().startswith("#"))

    def find(pattern: str) -> str | None:
        m = re.search(pattern, code)
        return m.group(1) if m else None

    cpu = find(r"\.cpus\s*=\s*(\d+)")
    mem = find(r"\.memory\s*=\s*(\d+)")
    return {
        "vm_name": find(r'vm\.define\s+["\']([^"\']+)["\']'),
        "box": find(r'vm\.box\s*=\s*["\']([^"\']+)["\']'),
        "ip": find(r'\bip:\s*["\']([^"\']+)["\']'),
        "bridge": find(r'\bbridge:\s*["\']([^"\']+)["\']'),
        "cpu": int(cpu) if cpu else None,
        "memory_mb": int(mem) if mem else None,
    }


def parse_ids(lines: list[str]) -> list[str]:
    return [l[3:].strip() for l in lines if l.startswith("ID:")]


def parse_status(lines: list[str]) -> tuple[str | None, str | None]:
    """스크립트의 상태 줄에서 (상태, 에러) 를 뽑는다.

    줄 형식: `머신이름=상태` / `not_created` / `ERR:메시지` / `ID:uuid`, `MANAGED`(무시)
    """
    states: list[tuple[str, str]] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("ID:") or line == "MANAGED":
            continue
        if line.startswith("ERR:"):
            return None, line[4:].strip()
        if line == "not_created":
            states.append(("", "not_created"))
        elif "=" in line:
            machine, _, state = line.partition("=")
            states.append((machine, state))
    if not states:
        return None, "상태 정보를 찾지 못함"
    if len(states) == 1:
        return states[0][1], None
    return ",".join(f"{m}={s}" for m, s in states), None  # 머신이 여러 개인 경우


def _make_vm(host: HostConfig, root: str, raw: dict) -> tuple[VmInfo, list[str]]:
    path = raw["path"].rstrip("/")
    rel = path[len(root) + 1:] if path.startswith(root + "/") else path
    parts = [p for p in rel.split("/") if p]
    status, error = parse_status(raw["status"])
    managed = "MANAGED" in raw["status"]
    vm = VmInfo(
        name=parts[-1] if parts else path,
        project=parts[0] if len(parts) > 1 else None,
        host=host.name,
        managed=managed,
        status=status,
        path=path,
        error=error,
        **parse_vagrantfile("\n".join(raw["vagrantfile"])),
    )
    return vm, parse_ids(raw["status"])


def _query_host(host: HostConfig, managed_only: bool) -> HostVms:
    out = HostVms(host=host.name, address=host.address, reachable=False)
    root = scan_root_of(host)
    try:
        result = run_command(host, _build_command(root), timeout=_SSH_TIMEOUT_SEC)
    except Exception as e:
        out.error = f"{type(e).__name__}: {e}"
        return out

    if result.exit_code == 3:
        out.error = f"조회 폴더를 찾을 수 없음: {root}"
        return out
    if result.exit_code != 0:
        out.error = f"exit={result.exit_code}: {result.stderr.strip()[:200]}"
        return out

    out.reachable = True
    vms: list[VmInfo] = []
    matched_ids: set[str] = set()
    for raw in parse_vm_output(result.stdout):
        vm, ids = _make_vm(host, root, raw)
        vms.append(vm)
        matched_ids.update(ids)

    out.vms = [v for v in vms if v.managed] if managed_only else vms
    if not managed_only:
        out.unmatched_running = [r for r in parse_running(result.stdout) if r.uuid not in matched_ids]
    return out


def list_vms(host: str | None = None, managed_only: bool = False) -> list[HostVms]:
    """ 호스트 여러 대를 ThreadPoolExcecutor로 병렬 조회"""
    hosts = get_settings().hosts if host is None else [find_host(host)]
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        return list(pool.map(lambda h: _query_host(h, managed_only), hosts))