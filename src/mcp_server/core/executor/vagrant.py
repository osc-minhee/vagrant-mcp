# mcp_server/core/executor/vagrant.py
import asyncio
import base64
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from mcp_server.core.executor.base import ExecResult, InfraExecutor
from mcp_server.core.executor.runner import sh_path
from mcp_server.core.hosts import VagrantHost

# 리소스마다 {호스트 workdir}/{resource_id} 폴더를 만들고 그 안에 Vagrantfile 을 둔다.
# 이미 실행 중이면 reload(사양 변경 반영), 아니면 up(최초 생성 또는 기동).
_APPLY_SCRIPT = r"""
command -v vagrant >/dev/null 2>&1 || { echo "호스트에 vagrant 가 설치되어 있지 않습니다." >&2; exit 14; }
DIR=__DIR__
mkdir -p "$DIR" && cd "$DIR" || { echo "작업 폴더를 만들 수 없습니다: $DIR" >&2; exit 15; }
echo __VF_B64__ | base64 -d > Vagrantfile.new && mv Vagrantfile.new Vagrantfile || exit 15
STATE="$(vagrant status --machine-readable 2>/dev/null </dev/null | awk -F, '$3=="state" {print $4; exit}')"
echo "@@STATE=${STATE:-unknown}"
if [ "$STATE" = running ]; then
  vagrant reload 2>&1 </dev/null
else
  vagrant up 2>&1 </dev/null
fi
"""

_DESTROY_SCRIPT = r"""
DIR=__DIR__
[ -d "$DIR" ] || { echo "이미 삭제된 리소스입니다."; exit 0; }
cd "$DIR" || exit 15
if [ -f Vagrantfile ]; then
  command -v vagrant >/dev/null 2>&1 || { echo "호스트에 vagrant 가 설치되어 있지 않습니다." >&2; exit 14; }
  vagrant destroy -f 2>&1 </dev/null || exit $?
fi
cd / && rm -rf -- "$DIR"
"""

# 호스트의 CPU/메모리/디스크 여유와 VirtualBox 에서 실행 중인 VM 의 할당 합계를 KEY=값 으로 출력한다.
# workdir 는 아직 없을 수 있으므로(첫 생성 때 만든다) 존재하는 가장 가까운 상위 폴더의 디스크를 본다.
_INSPECT_SCRIPT = r"""
D=__WD__
while [ ! -d "$D" ] && [ "$D" != "/" ] && [ "$D" != "." ]; do D="$(dirname "$D")"; done
echo "CPU_TOTAL=$(nproc)"
echo "LOAD1=$(cut -d' ' -f1 /proc/loadavg)"
awk '/^MemTotal:/ {print "MEM_TOTAL=" int($2/1024)} /^MemAvailable:/ {print "MEM_AVAIL=" int($2/1024)}' /proc/meminfo
df -P -m "$D" | awk 'NR==2 {print "DISK_TOTAL="$2; print "DISK_AVAIL="$4}'
if command -v vagrant >/dev/null 2>&1; then echo "VAGRANT=$(vagrant --version 2>/dev/null </dev/null | head -n1)"; fi
if command -v VBoxManage >/dev/null 2>&1; then
  echo "VBOX=$(VBoxManage --version 2>/dev/null </dev/null | head -n1)"
  for id in $(VBoxManage list runningvms 2>/dev/null </dev/null | sed -n 's/.*{\(.*\)}$/\1/p'); do
    VBoxManage showvminfo "$id" --machinereadable 2>/dev/null </dev/null | grep -E '^(cpus|memory)='
  done | awk -F= '/^cpus=/ {c+=$2; n++} /^memory=/ {m+=$2} END {print "VM_RUNNING=" n+0; print "VM_CPU_ALLOC=" c+0; print "VM_MEM_ALLOC_MB=" m+0}'
fi
true
"""

class HostInspectError(RuntimeError):
    """
    호스트에는 접속했지만 자원 조회 결과를 얻거나 해석하지 못했을 때 (접속 실패는 이 예외가 아니다).
    """

class VagrantExecutor(InfraExecutor):
    """
    Vagrantfile 을 렌더링해 원격(또는 로컬) 호스트에서 vagrant 명령을 실행하는 클래스.
    호스트가 여러 대면 리소스 vars 의 "host" 로 실행할 호스트를 고른다.
    """
    def __init__(
        self,
        hosts: Dict[str, VagrantHost],
        template_path: Path,
        ssh_pubkey_paths: List[Path],
        timeout: int = 1800,
        max_parallel: int = 2,
    ):
        if not hosts:
            raise ValueError("Vagrant 호스트가 하나 이상 필요합니다.")
        self.hosts = hosts
        # 입력 순서상 첫 번째 호스트. vars 에 host 기록이 없는 기존 리소스는 이 호스트에 있는 것으로 본다.
        self.default_host = next(iter(hosts.values()))
        self.template_path = template_path
        self.ssh_pubkey_paths = ssh_pubkey_paths
        self.timeout = timeout
        # 동시에 너무 많은 VM 을 올리면 box 다운로드/VirtualBox 가 충돌하므로 호스트마다 동시 실행 수를 제한한다.
        self.semaphores = {key: asyncio.Semaphore(max_parallel) for key in hosts}
        self.logger = logging.getLogger(__name__)

        self.jinja = Environment(
            loader=FileSystemLoader(str(template_path.parent)),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def host_for(self, name: Optional[str]) -> VagrantHost:
        """
        호스트 이름(대소문자 무시)으로 호스트를 찾는다. 이름이 없으면 기본 호스트.
        """
        if not name:
            return self.default_host
        host = self.hosts.get(str(name).lower())
        if not host:
            raise RuntimeError(f"호스트 '{name}' 설정을 찾을 수 없습니다. (사용 가능: {', '.join(h.name for h in self.hosts.values())})")
        return host

    def _vm_dir(self, host: VagrantHost, resource_id: str, vars: Dict[str, Any]) -> str:
        # project 는 생성 때 vars 에 기록된다. 기록이 없는 기존 리소스는 workdir 바로 아래에 있다.
        # (호스트의 default_project 로 대신하면 안 된다. 나중에 바뀐 설정 때문에 엉뚱한 경로를 지울 수 있다.)
        project = vars.get("project")
        base = f"{host.workdir}/{project}" if project else host.workdir
        return f"{base}/{resource_id}"

    def _ssh_pubkeys(self) -> List[str]:
        keys = []
        for path in self.ssh_pubkey_paths:
            if path.exists():
                key = path.read_text().strip()
                if "'" in key or "\n" in key:
                    raise ValueError(f"공개키 형식이 올바르지 않습니다: {path}")
                keys.append(key)

        if not keys:
            raise RuntimeError(f"VM 에 넣을 SSH 공개키가 없습니다: {', '.join(map(str, self.ssh_pubkey_paths))}")
        return keys

    def render(self, resource_id: str, vars: Dict[str, Any]) -> str:
        template = self.jinja.get_template(self.template_path.name)
        return template.render(resource_id=resource_id, ssh_pubkeys=self._ssh_pubkeys(), **vars)

    async def _run(self, host: VagrantHost, resource_id: str, command: str, script: str) -> ExecResult:
        async with self.semaphores[host.name.lower()]:
            self.logger.info(f"[{resource_id}] ({host.name}) vagrant {command} 실행 중...")
            result = await host.runner.run(script, timeout=self.timeout)

        success = result.exit_code == 0
        lines = result.stdout.splitlines()
        state = next((l.split("=", 1)[1] for l in lines if l.startswith("@@STATE=")), None)
        if state:
            self.logger.info(f"[{resource_id}] 실행 전 머신 상태: {state}")
        out = "\n".join(l for l in lines if not l.startswith("@@STATE=")).strip()
        err = result.stderr.strip()

        if not success:
            self.logger.error(
                f"[{resource_id}] vagrant {command} 실패 (ExitCode: {result.exit_code})\n"
                f"Error Details: {(err or out)[-2000:]}"
            )
        return success, out, err

    async def apply(self, resource_id: str, vars: Dict[str, Any]) -> ExecResult:
        host = self.host_for(vars.get("host"))
        vagrantfile = self.render(resource_id, vars)
        script = (
            _APPLY_SCRIPT
            .replace("__DIR__", sh_path(self._vm_dir(host, resource_id, vars)))
            .replace("__VF_B64__", base64.b64encode(vagrantfile.encode()).decode())
        )
        return await self._run(host, resource_id, "up/reload", script)

    async def destroy(self, resource_id: str, vars: Dict[str, Any]) -> ExecResult:
        host = self.host_for(vars.get("host"))
        script = _DESTROY_SCRIPT.replace("__DIR__", sh_path(self._vm_dir(host, resource_id, vars)))
        return await self._run(host, resource_id, "destroy", script)

    async def is_ip_in_use(self, host: VagrantHost, ip: str) -> bool:
        """
        해당 Vagrant 호스트에서 ping 으로 IP 사용 여부를 확인한다. (DB 밖에서 쓰고 있는 IP 를 피하기 위함)
        """
        result = await host.runner.run(f"ping -c1 -W1 {ip} >/dev/null 2>&1", timeout=10)
        return result.exit_code == 0

    async def inspect_host(self, host: VagrantHost) -> Dict[str, Any]:
        """
        호스트의 CPU/메모리/디스크 여유와 VirtualBox 실행 현황을 조회한다. (읽기 전용)
        접속 실패 등은 예외로 올라가므로 호출하는 쪽에서 처리한다. VirtualBox 가 없으면 vbox_* 값은 None.
        메모리/디스크 단위는 MiB 이다.
        """
        result = await host.runner.run(_INSPECT_SCRIPT.replace("__WD__", sh_path(host.workdir)), timeout=30)
        if result.exit_code != 0:
            raise HostInspectError(f"호스트 조회 실패 (exit={result.exit_code}): {result.stderr.strip()[:200]}")

        values: Dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                values[key.strip()] = value.strip()

        try:
            cpu_total = int(values["CPU_TOTAL"])
            load = float(values["LOAD1"])
            info = {
                "cpu_total": cpu_total,
                "load_1min": load,
                # 부하 기반 추정치. VM 에 할당된 vCPU 합계가 아니다.
                "cpu_free_estimate": round(max(cpu_total - load, 0.0), 2),
                "mem_total_mb": int(values["MEM_TOTAL"]),
                "mem_available_mb": int(values["MEM_AVAIL"]),
                "disk_total_mb": int(values["DISK_TOTAL"]),
                "disk_available_mb": int(values["DISK_AVAIL"]),
            }
        except (KeyError, ValueError) as e:
            raise HostInspectError(f"호스트 조회 결과를 해석하지 못했습니다: {e!r}") from e

        info["vagrant_version"] = values.get("VAGRANT") or None
        info["virtualbox_version"] = values.get("VBOX") or None
        # VirtualBox 에서 실제로 실행 중인 VM 전체(다른 사람 VM 포함)의 합계
        for out_key, key in (("vbox_running_vms", "VM_RUNNING"), ("vbox_vcpu_allocated", "VM_CPU_ALLOC"), ("vbox_mem_allocated_mb", "VM_MEM_ALLOC_MB")):
            info[out_key] = int(values[key]) if values.get(key, "").isdigit() else None
        return info
