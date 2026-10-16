"""host_list 로직: 각 호스트의 CPU / 메모리 / 디스크 여유 + VM 할당 합계 조회."""

import shlex
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, computed_field

from .config import HostConfig, get_settings, to_gi
from .ssh import run_command

# 메모리는 free 대신 /proc/meminfo 를 쓴다 (한글 로케일에서 free 출력이 "메모리:" 로 나와 깨짐).
# VM 할당 합계는 VirtualBox 에서 "실행 중인 VM" 의 cpus/memory 를 직접 합산한다.
_SCRIPT = r"""
[ -d __WD__ ] || { echo "workdir not found" >&2; exit 4; }
echo "CPU_TOTAL=$(nproc)"
echo "LOAD1=$(cut -d' ' -f1 /proc/loadavg)"
awk '/^MemTotal:/ {print "MEM_TOTAL=" int($2/1024)} /^MemAvailable:/ {print "MEM_AVAIL=" int($2/1024)}' /proc/meminfo
df -P -m __WD__ | awk 'NR==2 {print "DISK_TOTAL="$2; print "DISK_AVAIL="$4}'
if command -v VBoxManage >/dev/null 2>&1; then
  for id in $(VBoxManage list runningvms 2>/dev/null </dev/null | sed -n 's/.*{\(.*\)}$/\1/p'); do
    VBoxManage showvminfo "$id" --machinereadable 2>/dev/null </dev/null | grep -E '^(cpus|memory)='
  done | awk -F= '/^cpus=/ {c+=$2; n++} /^memory=/ {m+=$2} END {print "VM_RUNNING=" n+0; print "VM_CPU_ALLOC=" c+0; print "VM_MEM_ALLOC_MB=" m+0}'
fi
true
"""


class HostInfo(BaseModel):
    name: str
    address: str
    reachable: bool
    error: str | None = None

    cpu_total: int | None = None
    load_1min: float | None = None
    # 부하 기반 추정치 (cpu_total - load_1min). VM 에 할당된 vCPU 합산이 아님에 주의
    cpu_free_estimate: float | None = None

    mem_total_mb: int | None = None
    mem_available_mb: int | None = None

    disk_total_mb: int | None = None
    disk_available_mb: int | None = None

    # VirtualBox 에서 실제로 "실행 중인" VM 들의 합계 (이 호스트의 모든 VM 포함)
    vm_running_count: int | None = None
    vcpu_allocated: int | None = None
    mem_allocated_mb: int | None = None

    # 사람이 보는 단위 (GiB, free -h 와 같음). 계산은 *_mb 로 한다
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
    def disk_total_gi(self) -> float | None:
        return to_gi(self.disk_total_mb)

    @computed_field
    def disk_available_gi(self) -> float | None:
        return to_gi(self.disk_available_mb)


def _build_command(workdir: str) -> str:
    return _SCRIPT.replace("__WD__", shlex.quote(workdir))


def parse_host_output(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def query_host(host: HostConfig) -> HostInfo:
    base = HostInfo(name=host.name, address=host.address, reachable=False)
    try:
        result = run_command(host, _build_command(host.workdir))
    except Exception as e:  # 접속 실패, 타임아웃, 인증 실패 등
        base.error = f"{type(e).__name__}: {e}"
        return base

    if result.exit_code == 4:
        base.error = f"작업 폴더를 찾을 수 없음: {host.workdir}"
        return base
    if result.exit_code != 0:
        base.error = f"exit={result.exit_code}: {result.stderr.strip()[:200]}"
        return base

    v = parse_host_output(result.stdout)
    try:
        cpu_total = int(v["CPU_TOTAL"])
        load = float(v["LOAD1"])
        base.reachable = True
        base.cpu_total = cpu_total
        base.load_1min = load
        base.cpu_free_estimate = round(max(cpu_total - load, 0.0), 2)
        base.mem_total_mb = int(v["MEM_TOTAL"])
        base.mem_available_mb = int(v["MEM_AVAIL"])
        base.disk_total_mb = int(v["DISK_TOTAL"])
        base.disk_available_mb = int(v["DISK_AVAIL"])
        # VirtualBox 가 없는 호스트에서는 아래 값이 없다 -> None 으로 둔다
        if "VM_RUNNING" in v:
            base.vm_running_count = int(v["VM_RUNNING"])
            base.vcpu_allocated = int(v["VM_CPU_ALLOC"])
            base.mem_allocated_mb = int(v["VM_MEM_ALLOC_MB"])
    except (KeyError, ValueError) as e:
        base.reachable = False
        base.error = f"parse error: {e!r}, raw={result.stdout[:200]!r}"
    return base


def list_hosts() -> list[HostInfo]:
    hosts = get_settings().hosts
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        return list(pool.map(query_host, hosts))