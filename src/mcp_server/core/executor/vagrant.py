# mcp_server/core/executor/vagrant.py
import asyncio
import base64
import logging
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from mcp_server.core.executor.base import ExecResult, InfraExecutor, VerifyResult
from mcp_server.core.executor.runner import sh_path
from mcp_server.core.hosts import VagrantHost
from mcp_server.core.redact import redact_text

# 리소스마다 {호스트 workdir}/{resource_id} 폴더를 만들고 그 안에 Vagrantfile 을 둔다.
# 이미 실행 중이면 reload(사양 변경 반영), 아니면 up(최초 생성 또는 기동).
_APPLY_SCRIPT = r"""
command -v vagrant >/dev/null 2>&1 || { echo "호스트에 vagrant 가 설치되어 있지 않습니다." >&2; exit 14; }
__EXPERIMENTAL__
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

# VM 폴더를 지운 뒤, project 폴더(PROJ)가 비었으면 함께 지운다. rmdir 은 빈 폴더만 지우므로 다른 VM/파일이 있으면 남는다.
# PROJ 는 project 가 있을 때만 넘긴다(비어 있으면 건너뜀). workdir 은 지우지 않는다.
_DESTROY_SCRIPT = r"""
DIR=__DIR__
PROJ=__PROJ__
if [ -d "$DIR" ]; then
  cd "$DIR" || exit 15
  if [ -f Vagrantfile ]; then
    command -v vagrant >/dev/null 2>&1 || { echo "호스트에 vagrant 가 설치되어 있지 않습니다." >&2; exit 14; }
    __EXPERIMENTAL__
    vagrant destroy -f 2>&1 </dev/null || exit $?
  fi
  cd / && rm -rf -- "$DIR" || exit 15
else
  echo "이미 삭제된 리소스입니다."
fi
if [ -n "$PROJ" ] && [ -d "$PROJ" ]; then
  rmdir -- "$PROJ" 2>/dev/null && echo "비어 있는 project 폴더를 정리했습니다."
fi
true
"""

# Vagrantfile 의 config.vm.disk(디스크 크기)는 Vagrant 실험 기능이라 켜 둔다. 호스트에 이미 설정된 값이 있으면 뒤에 붙인다.
_EXPERIMENTAL = 'export VAGRANT_EXPERIMENTAL="${VAGRANT_EXPERIMENTAL:+$VAGRANT_EXPERIMENTAL,}disks"'

# 호스트의 CPU/메모리/디스크 여유와 VirtualBox 에서 실행 중인 VM 의 할당 합계를 KEY=값 으로 출력한다.
# 디스크는 용도별로 본다. VM 디스크(.vdi/.vmdk)는 workdir 가 아니라 VirtualBox 의 Default machine folder 에 생기므로
# 그 폴더(VMDIR, 설정값이 없으면 VBoxManage 로 확인)의 파일시스템이 VM 용량 기준이다.
#  WD_*: workdir (Vagrantfile, .vagrant)   VM_*: VM 디스크 폴더   BOX_*: Vagrant box 캐시 (VAGRANT_HOME, 기본 ~/.vagrant.d)
# 폴더는 아직 없을 수 있으므로(첫 생성 때 만든다) 존재하는 가장 가까운 상위 폴더의 디스크를 본다.
_INSPECT_SCRIPT = r"""
disk() {
  D="$1"
  while [ ! -d "$D" ] && [ "$D" != "/" ] && [ "$D" != "." ]; do D="$(dirname "$D")"; done
  # 장치 이름이나 마운트 경로에 공백이 있을 수 있으므로 뒤에서부터 센다. (뒤 6번째 열부터는 Filesystem 이름의 일부)
  df -P -m "$D" 2>/dev/null | awk -v k="$2" 'NR>=2 {line=$0} END {
    n=split(line, f, /[ \t]+/); for (i=n-4; i>=1; i--) if (f[i+3] ~ /%$/) {print k"_TOTAL="f[i]; print k"_AVAIL="f[i+2]; m=f[i+4]; for (j=i+5; j<=n; j++) m=m" "f[j]; print k"_MOUNT="m; exit}
  }'
}
echo "CPU_TOTAL=$(nproc)"
echo "LOAD1=$(cut -d' ' -f1 /proc/loadavg)"
awk '/^MemTotal:/ {print "MEM_TOTAL=" int($2/1024)} /^MemAvailable:/ {print "MEM_AVAIL=" int($2/1024)}' /proc/meminfo
disk __WD__ WD
VMDIR=__VMDIR__
if [ -z "$VMDIR" ] && command -v VBoxManage >/dev/null 2>&1; then
  VMDIR="$(VBoxManage list systemproperties 2>/dev/null </dev/null | sed -n 's/^Default machine folder:[[:space:]]*//p' | head -n1)"
fi
[ -n "$VMDIR" ] && disk "$VMDIR" VM
disk "${VAGRANT_HOME:-$HOME/.vagrant.d}" BOX
disk / ROOT
if command -v vagrant >/dev/null 2>&1; then echo "VAGRANT=$(vagrant --version 2>/dev/null </dev/null | head -n1)"; fi
if command -v VBoxManage >/dev/null 2>&1; then
  echo "VBOX=$(VBoxManage --version 2>/dev/null </dev/null | head -n1)"
  for id in $(VBoxManage list runningvms 2>/dev/null </dev/null | sed -n 's/.*{\(.*\)}$/\1/p'); do
    VBoxManage showvminfo "$id" --machinereadable 2>/dev/null </dev/null | grep -E '^(cpus|memory)='
  done | awk -F= '/^cpus=/ {c+=$2; n++} /^memory=/ {m+=$2} END {print "VM_RUNNING=" n+0; print "VM_CPU_ALLOC=" c+0; print "VM_MEM_ALLOC_MB=" m+0}'
fi
true
"""

# 호스트에서 사용 중인 IP 를 찾는다. (읽기 전용)
#  VF=<ip>    : scan_root 아래 Vagrantfile 에 적힌 고정 IP (꺼진 VM, 다른 사람이 만든 VM 포함. 주석 줄도 보수적으로 포함)
#  VB=<ip>    : SSH 계정으로 실행 중인 VirtualBox VM 의 게스트 IP (Guest Additions 가 있어야 보인다)
#  NOROOT=1   : scan_root 폴더가 없음
_SCAN_IPS_SCRIPT = r"""
ROOT=__ROOT__
if [ -d "$ROOT" ]; then
  find "$ROOT" -maxdepth 6 \( -name .vagrant -o -name .git -o -name node_modules \) -prune -o -name Vagrantfile -type f -print0 2>/dev/null \
    | xargs -0 -r grep -hoE "ip:[[:space:]]*[\"'][0-9]+(\.[0-9]+){3}[\"']" 2>/dev/null \
    | grep -oE '[0-9]+(\.[0-9]+){3}' | sed 's/^/VF=/'
else
  echo "NOROOT=1"
fi
if command -v VBoxManage >/dev/null 2>&1; then
  for id in $(VBoxManage list runningvms 2>/dev/null </dev/null | sed -n 's/.*{\(.*\)}$/\1/p'); do
    VBoxManage guestproperty enumerate "$id" 2>/dev/null </dev/null \
      | grep -oE '/V4/IP[^0-9]*[0-9]+(\.[0-9]+){3}' | grep -oE '[0-9]+(\.[0-9]+){3}$' | sed 's/^/VB=/'
  done
fi
true
"""

# 생성/변경 후 확인: VirtualBox 에서 VM(vb.name = resource_id)의 상태와, 호스트에서 VM IP 로 ping 이 되는지
_VERIFY_UP_SCRIPT = r"""
command -v VBoxManage >/dev/null 2>&1 || { echo "호스트에 VBoxManage 가 없습니다." >&2; exit 23; }
ST="$(VBoxManage showvminfo __NAME__ --machinereadable 2>/dev/null </dev/null | sed -n 's/^VMState="\(.*\)"$/\1/p')"
echo "STATE=${ST:-missing}"
IP=__IP__
if [ -n "$IP" ]; then
  ok=0
  for i in 1 2 3 4 5; do ping -c1 -W2 "$IP" >/dev/null 2>&1 && { ok=1; break; }; sleep 3; done
  echo "PING=$ok"
fi
"""

# 삭제 후 확인: VM 폴더와 VirtualBox VM 이 모두 없어야 한다
_VERIFY_GONE_SCRIPT = r"""
[ -e __DIR__ ] && echo "DIR=1" || echo "DIR=0"
if command -v VBoxManage >/dev/null 2>&1; then
  VBoxManage showvminfo __NAME__ >/dev/null 2>&1 </dev/null && echo "VM=1" || echo "VM=0"
else
  echo "VM=unknown"
fi
"""

# 호스트의 VirtualBox 에 등록된 VM 목록 (SSH 계정의 VM 만 보인다). __ONLY__ 가 있으면 그 VM 하나만 본다. (읽기 전용)
#  @@VM=<uuid> 로 VM 하나가 시작되고, 이어서 name/VMState/cpus/memory 와 IP=<게스트 IP> (실행 중 + Guest Additions) 가 온다.
_LIST_VMS_SCRIPT = r"""
command -v VBoxManage >/dev/null 2>&1 || { echo "호스트에 VBoxManage 가 없습니다." >&2; exit 23; }
ONLY=__ONLY__
if [ -n "$ONLY" ]; then
  IDS="$ONLY"
else
  IDS="$(VBoxManage list vms 2>/dev/null </dev/null | sed -n 's/.*{\(.*\)}$/\1/p')"
fi
for id in $IDS; do
  INFO="$(VBoxManage showvminfo "$id" --machinereadable 2>/dev/null </dev/null)" || continue
  echo "@@VM=$id"
  printf '%s\n' "$INFO" | grep -E '^(name|VMState|cpus|memory)='
  VBoxManage guestproperty enumerate "$id" 2>/dev/null </dev/null \
    | grep -oE '/V4/IP[^0-9]*[0-9]+(\.[0-9]+){3}' | grep -oE '[0-9]+(\.[0-9]+){3}$' | sed 's/^/IP=/'
done
true
"""

# 계정과 상관없이 호스트의 VM 을 찾는다. (읽기 전용)
#  PS=<계정> <명령줄> : 실행 중인 VirtualBox VM 프로세스 (모든 계정. --startvm 뒤가 VM UUID, --comment 뒤가 VM 이름)
#  VF=<폴더>          : scan_root 아래 Vagrantfile 이 있는 폴더. 이어서 OWNER(파일 소유자), VFIP(적힌 고정 IP),
#                       MACHINE/MID(.vagrant/machines/<머신>/virtualbox/id 의 VM UUID. 읽지 못하면 '?'. id 가 없으면 destroy 됐거나 만든 적 없음),
#                       NOACCESS(.vagrant/machines 를 읽을 권한이 없음)
#  NOROOT=1           : scan_root 폴더가 없음
# grep 패턴을 [V]BoxHeadless 로 쓰면 이 스크립트 자신(bash -c 의 명령줄)은 걸리지 않는다.
_SCAN_VMS_SCRIPT = r"""
ps -eo user=,args= 2>/dev/null | grep -E '[V]BoxHeadless|[V]irtualBoxVM' | sed 's/^/PS=/'
ROOT=__ROOT__
if [ -d "$ROOT" ]; then
  find "$ROOT" -maxdepth 6 \( -name .vagrant -o -name .git -o -name node_modules \) -prune -o -name Vagrantfile -type f -print 2>/dev/null \
  | while IFS= read -r vf; do
      d="$(dirname "$vf")"
      echo "VF=$d"
      echo "OWNER=$(stat -c %U "$vf" 2>/dev/null)"
      grep -hoE "ip:[[:space:]]*[\"'][0-9]+(\.[0-9]+){3}[\"']" "$vf" 2>/dev/null | grep -oE '[0-9]+(\.[0-9]+){3}' | sed 's/^/VFIP=/'
      if [ -d "$d/.vagrant/machines" ] && ! ls "$d/.vagrant/machines" >/dev/null 2>&1; then echo "NOACCESS=1"; fi
      for m in "$d"/.vagrant/machines/*/virtualbox; do
        [ -f "$m/id" ] || continue
        echo "MACHINE=$(basename "$(dirname "$m")")"
        echo "MID=$(cat "$m/id" 2>/dev/null || echo '?')"
      done
    done
else
  echo "NOROOT=1"
fi
true
"""

# Vagrant 가 모든 VM 에 붙이는 NAT 주소. VM 을 구분하는 데 쓸모가 없어 목록에서 뺀다.
_VAGRANT_NAT_IP = "10.0.2.15"

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
        boot_timeout: int = 600,
    ):
        if not hosts:
            raise ValueError("Vagrant 호스트가 하나 이상 필요합니다.")
        self.hosts = hosts
        # 입력 순서상 첫 번째 호스트
        self.default_host = next(iter(hosts.values()))
        self.template_path = template_path
        self.ssh_pubkey_paths = ssh_pubkey_paths
        self.timeout = timeout
        # vagrant up 이 VM 부팅(SSH 접속)을 기다리는 시간 (Vagrantfile 의 config.vm.boot_timeout, Vagrant 기본 300초)
        self.boot_timeout = boot_timeout
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

    def host_of(self, vars: Dict[str, Any]) -> VagrantHost:
        """
        리소스가 있는 호스트. 생성 때 기록한 host 를 쓰고, 기록이 없는 기존 리소스는 IP 가 속한 ip_range 의 호스트로 찾는다.
        (설정 순서에 기대지 않는다. 순서가 바뀌면 엉뚱한 호스트에서 삭제할 수 있다.) 찾지 못하거나 여러 곳이면 RuntimeError.
        """
        if vars.get("host"):
            return self.host_for(vars["host"])
        if len(self.hosts) == 1:
            return self.default_host

        ip = str(vars.get("ip") or "")
        matches = [h for h in self.hosts.values() if ip and ip in h.ip_pool]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise RuntimeError(f"호스트 기록이 없고 IP({ip or '없음'})가 어느 호스트의 ip_ranges 에도 속하지 않아 호스트를 정할 수 없습니다.")
        raise RuntimeError(f"호스트 기록이 없고 IP({ip})가 여러 호스트의 ip_ranges 에 속해 호스트를 정할 수 없습니다: {', '.join(h.name for h in matches)}")

    def _vm_dir(self, host: VagrantHost, resource_id: str, vars: Dict[str, Any]) -> str:
        # project 와 dir_name 은 생성 때 vars 에 기록된다. 기록이 없는 기존 리소스는 workdir 바로 아래의 resource_id 폴더에 있다.
        # (호스트의 default_project 로 대신하면 안 된다. 나중에 바뀐 설정 때문에 엉뚱한 경로를 지울 수 있다.)
        project = vars.get("project")
        base = f"{host.workdir}/{project}" if project else host.workdir
        return f"{base}/{vars.get('dir_name') or resource_id}"

    async def vm_dir_exists(self, host: VagrantHost, resource_id: str, vars: Dict[str, Any]) -> bool:
        """
        VM 폴더가 호스트에 이미 있는지. (읽기 전용) 남의 폴더를 덮어쓰거나 삭제 때 지우지 않도록 생성 전에 확인한다.
        """
        result = await host.runner.run(f"[ -e {sh_path(self._vm_dir(host, resource_id, vars))} ] && echo 1 || echo 0", timeout=30)
        return result.stdout.strip() == "1"

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
        return template.render(
            resource_id=resource_id, ssh_pubkeys=self._ssh_pubkeys(), boot_timeout=self.boot_timeout, **vars
        )

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
        host = self.host_of(vars)
        vagrantfile = self.render(resource_id, vars)
        script = (
            _APPLY_SCRIPT
            .replace("__EXPERIMENTAL__", _EXPERIMENTAL)
            .replace("__DIR__", sh_path(self._vm_dir(host, resource_id, vars)))
            .replace("__VF_B64__", base64.b64encode(vagrantfile.encode()).decode())
        )
        return await self._run(host, resource_id, "up/reload", script)

    async def destroy(self, resource_id: str, vars: Dict[str, Any]) -> ExecResult:
        host = self.host_of(vars)
        # project 가 있으면 VM 폴더를 지운 뒤 그 project 폴더가 비었을 때 정리한다. (project 가 없으면 상위는 workdir 이라 건드리지 않는다)
        project = vars.get("project")
        project_dir = sh_path(f"{host.workdir}/{project}") if project else "''"
        script = (
            _DESTROY_SCRIPT
            .replace("__EXPERIMENTAL__", _EXPERIMENTAL)
            .replace("__DIR__", sh_path(self._vm_dir(host, resource_id, vars)))
            .replace("__PROJ__", project_dir)
        )
        return await self._run(host, resource_id, "destroy", script)


    @staticmethod
    def _kv(text: str) -> Dict[str, str]:
        values: Dict[str, str] = {}
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                values[key.strip()] = value.strip()
        return values

    async def verify_applied(self, resource_id: str, vars: Dict[str, Any]) -> VerifyResult:
        """
        VirtualBox 에서 VM 이 running 인지 확인한다. (아니면 실패)
        VM IP 가 ping 에 응답하지 않으면 실패로 보지 않고 경고로 돌려준다. (bridge 설정이 틀린 경우가 많다)
        """
        host = self.host_of(vars)
        ip = vars.get("ip") or ""
        script = _VERIFY_UP_SCRIPT.replace("__NAME__", shlex.quote(resource_id)).replace("__IP__", shlex.quote(ip))
        result = await host.runner.run(script, timeout=60)
        if result.exit_code != 0:
            return False, (redact_text(result.stderr.strip()) or f"확인 스크립트 실패 (exit={result.exit_code})")[:300]

        values = self._kv(result.stdout)
        state = values.get("STATE", "missing")
        if state != "running":
            return False, f"VirtualBox 에서 VM '{resource_id}' 의 상태가 '{state}' 입니다. (running 이어야 합니다)"
        if values.get("PING") == "0":
            return True, f"VM 은 실행 중이지만 IP {ip} 가 호스트에서 ping 에 응답하지 않습니다. 호스트의 bridge(NIC) 설정을 확인하세요."
        return True, ""

    async def verify_destroyed(self, resource_id: str, vars: Dict[str, Any]) -> VerifyResult:
        """
        VM 폴더와 VirtualBox VM 이 모두 사라졌는지 확인한다. 하나라도 남아 있으면 실패.
        (폴더가 없어서 '이미 삭제됨' 으로 끝났는데 VM 은 실행 중인 경우를 잡는다. 예: workdir 설정이 바뀐 경우)
        """
        host = self.host_of(vars)
        vm_dir = self._vm_dir(host, resource_id, vars)
        script = _VERIFY_GONE_SCRIPT.replace("__DIR__", sh_path(vm_dir)).replace("__NAME__", shlex.quote(resource_id))
        result = await host.runner.run(script, timeout=60)
        values = self._kv(result.stdout)

        if values.get("VM") == "1":
            return False, (
                f"VirtualBox 에 VM '{resource_id}' 가 아직 남아 있습니다. VM 폴더 경로({vm_dir})가 생성 때와 다를 수 있으니 "
                "workdir/project 설정이 바뀌지 않았는지 확인하세요. 기록은 지우지 않았습니다."
            )
        if values.get("DIR") == "1":
            return False, f"VM 폴더가 남아 있습니다: {vm_dir}"
        if values.get("VM") != "0":
            return True, "VBoxManage 가 없어 VirtualBox 에서 사라졌는지는 확인하지 못했습니다."
        return True, ""

    async def used_ips(self, host: VagrantHost) -> Dict[str, str]:
        """
        호스트에서 사용 중인 IP 를 {IP: 출처("vagrantfile" | "virtualbox")} 로 돌려준다. (읽기 전용)
        접속 실패는 예외로 올라간다. scan_root 가 없으면 Vagrantfile 검색만 건너뛴다.
        """
        result = await host.runner.run(_SCAN_IPS_SCRIPT.replace("__ROOT__", sh_path(host.scan_root)), timeout=60)
        if result.exit_code != 0:
            raise HostInspectError(f"사용 중 IP 조회 실패 (exit={result.exit_code}): {redact_text(result.stderr.strip())[:200]}")

        found: Dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, _, value = line.strip().partition("=")
            if key == "NOROOT":
                self.logger.warning(f"[{host.name}] scan_root({host.scan_root}) 폴더가 없어 Vagrantfile 의 IP 는 확인하지 못했습니다.")
            elif key == "VF" and value:
                found.setdefault(value, "vagrantfile")
            elif key == "VB" and value:
                found.setdefault(value, "virtualbox")
        return found

    async def list_vms(self, host: VagrantHost, only: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        호스트의 VirtualBox 에 등록된 VM 을 [{id, name, state, cpus, memory_mb, guest_ips}] 로 돌려준다. (읽기 전용)
        SSH 계정의 VM 만 보인다. id 는 전체 조회면 VM UUID, only 조회면 only 값이다.
        only(VM 이름)를 주면 그 VM 만 보고, 없으면 빈 목록. 접속 실패는 예외로, VBoxManage 가 없으면 HostInspectError.
        """
        script = _LIST_VMS_SCRIPT.replace("__ONLY__", shlex.quote(only or ""))
        result = await host.runner.run(script, timeout=120)
        if result.exit_code != 0:
            raise HostInspectError(f"VM 목록 조회 실패 (exit={result.exit_code}): {result.stderr.strip()[:200]}")

        vms: List[Dict[str, Any]] = []
        for line in result.stdout.splitlines():
            key, sep, value = line.strip().partition("=")
            if not sep:
                continue
            value = value.strip().strip('"')
            if key == "@@VM":
                vms.append({"id": value, "name": value, "state": None, "cpus": None, "memory_mb": None, "guest_ips": []})
            elif not vms:
                continue
            elif key == "name":
                vms[-1]["name"] = value
            elif key == "VMState":
                vms[-1]["state"] = value
            elif key in ("cpus", "memory") and value.isdigit():
                vms[-1]["cpus" if key == "cpus" else "memory_mb"] = int(value)
            elif key == "IP" and value not in vms[-1]["guest_ips"]:
                vms[-1]["guest_ips"].append(value)
        return vms

    async def host_inventory(self, host: VagrantHost) -> Dict[str, Any]:
        """
        호스트의 VM 을 계정과 상관없이 모아 VM UUID 로 합친다. (읽기 전용)
        1) SSH 계정의 VirtualBox 등록 VM: 정확한 상태와 사양
        2) 실행 중인 VirtualBox 프로세스(모든 계정): 실행 여부와 실행한 계정
        3) scan_root 아래 Vagrantfile 과 .vagrant 기록(모든 계정, 읽기 권한이 있는 폴더): 프로젝트 폴더, 머신, 고정 IP
        다른 계정의 꺼진 VM 은 poweroff/saved 를 구분할 수 없어 state="not_running" 이다.
        반환: {"vms": [...], "notes": [조회하지 못한 부분 설명]}. 호스트 접속 실패는 예외로 올라간다.
        """
        async def own():
            try:
                return await self.list_vms(host), None
            except HostInspectError as e:
                return [], f"SSH 계정의 VirtualBox 목록을 읽지 못했습니다: {e}"

        (own_vms, own_error), result = await asyncio.gather(
            own(), host.runner.run(_SCAN_VMS_SCRIPT.replace("__ROOT__", sh_path(host.scan_root)), timeout=120)
        )
        notes = [own_error] if own_error else []
        if result.exit_code != 0:
            raise HostInspectError(f"VM 검색 실패 (exit={result.exit_code}): {redact_text(result.stderr.strip())[:200]}")

        vms: Dict[str, Dict[str, Any]] = {}

        def entry(key: str, **values) -> Dict[str, Any]:
            if key not in vms:
                vms[key] = {
                    "name": None, "state": None, "owner": None, "project_dir": None, "vagrant_machine": None,
                    "cpus": None, "memory_mb": None, "ips": [], "sources": [],
                }
            item = vms[key]
            for k, v in values.items():
                if item.get(k) is None:
                    item[k] = v
            return item

        def add(item: Dict[str, Any], source: str, ips=()) -> None:
            if source not in item["sources"]:
                item["sources"].append(source)
            for ip in ips:
                if ip != _VAGRANT_NAT_IP and ip not in item["ips"]:
                    item["ips"].append(ip)

        # 1) SSH 계정의 VM 은 상태와 사양이 정확하므로 먼저 채운다. (LocalRunner 면 계정을 모른다)
        ssh_user = getattr(host.runner, "user", None)
        for vm in own_vms:
            item = entry(vm["id"], name=vm["name"], state=vm["state"], cpus=vm["cpus"], memory_mb=vm["memory_mb"], owner=ssh_user)
            add(item, "virtualbox", vm["guest_ips"])

        def rel(path: str) -> str:
            # scan_root 기준 상대 경로 (호스트의 절대 경로는 보여 주지 않는다)
            root = host.scan_root.rstrip("/")
            if path == root:
                return "."
            return path[len(root) + 1:] if path.startswith(root + "/") else path

        current: Optional[Dict[str, Any]] = None
        machine: Optional[str] = None
        for line in result.stdout.splitlines():
            key, sep, value = line.partition("=")
            if not sep:
                continue
            value = value.strip()
            if key == "PS":
                user, _, args = value.partition(" ")
                vm_id = re.search(r"(?:^|\s)(?:--?startvm|-s)[= ](\S+)", args)
                if not vm_id:
                    continue
                comment = re.search(r"(?:^|\s)--?comment[= ](\S+)", args)
                item = entry(vm_id.group(1), name=comment.group(1) if comment else None, state="running", owner=user)
                add(item, "process")
            elif key == "NOROOT":
                notes.append("scan_root 폴더가 없어 Vagrantfile 은 찾지 못했습니다.")
            elif key == "VF":
                current = {"dir": rel(value), "owner": None, "ips": [], "machines": 0}
                # 머신 기록이 없으면 아직 vagrant up 하지 않은 프로젝트다. 머신이 나오면 이 항목은 지운다.
                current["key"] = f"vagrantfile:{value}"
                entry(current["key"], state="not_created", project_dir=current["dir"])
                add(vms[current["key"]], "vagrantfile")
            elif current is None:
                continue
            elif key == "OWNER":
                current["owner"] = value or None
                vms[current["key"]]["owner"] = current["owner"]
            elif key == "VFIP":
                current["ips"].append(value)
                add(vms[current["key"]], "vagrantfile", [value])
            elif key == "NOACCESS" and current["key"] in vms:
                # .vagrant 기록을 읽을 수 없어 만들어졌는지 알 수 없다.
                vms[current["key"]]["state"] = "unknown"
            elif key == "MACHINE":
                machine = value
            elif key == "MID":
                if current["machines"] == 0:
                    vms.pop(current["key"], None)
                current["machines"] += 1
                # id 를 읽지 못하면(권한) 상태를 알 수 없다. 읽었는데 1), 2) 에 없으면 꺼져 있다.
                readable = value not in ("", "?")
                item = entry(
                    value if readable else f"vagrantfile:{current['dir']}#{machine}",
                    state="not_running" if readable else "unknown",
                    owner=current["owner"],
                )
                item["project_dir"] = current["dir"]
                item["vagrant_machine"] = machine
                add(item, "vagrantfile", current["ips"])

        return {"vms": list(vms.values()), "notes": notes}

    async def inspect_host(self, host: VagrantHost) -> Dict[str, Any]:
        """
        호스트의 CPU/메모리/디스크 여유와 VirtualBox 실행 현황을 조회한다. (읽기 전용)
        접속 실패 등은 예외로 올라가므로 호출하는 쪽에서 처리한다. VirtualBox 가 없으면 vbox_* 값은 None.
        메모리/디스크 단위는 MiB 이다.
        """
        script = _INSPECT_SCRIPT.replace("__WD__", sh_path(host.workdir)).replace(
            "__VMDIR__", sh_path(host.vm_disk_path) if host.vm_disk_path else "''"
        )
        result = await host.runner.run(script, timeout=30)
        if result.exit_code != 0:
            raise HostInspectError(f"호스트 조회 실패 (exit={result.exit_code}): {redact_text(result.stderr.strip())[:200]}")

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
            }
            disks: Dict[str, Dict[str, Any]] = {}
            # root(/)는 다른 용도와 같은 파일시스템이어도 항상 목록에 나오도록 함께 잰다.
            for prefix, role in (("VM", "vm_disks"), ("WD", "workdir"), ("BOX", "boxes"), ("ROOT", "root")):
                if f"{prefix}_TOTAL" not in values:
                    continue
                mount = values.get(f"{prefix}_MOUNT") or "?"
                # 같은 파일시스템이면 한 항목으로 합친다.
                disks.setdefault(mount, {
                    "mount": redact_text(mount), "roles": [],
                    "total_mb": int(values[f"{prefix}_TOTAL"]), "available_mb": int(values[f"{prefix}_AVAIL"]),
                })["roles"].append(role)
            # VM 디스크 폴더를 찾지 못하면(VirtualBox 없음 등) workdir 의 디스크로 판단한다.
            basis = next(d for d in disks.values() if ("vm_disks" in d["roles"] or "workdir" in d["roles"]))
            info.update(
                disk_total_mb=basis["total_mb"],
                disk_available_mb=basis["available_mb"],
                disk_mount=basis["mount"],
                disk_basis="vm_disks" if "vm_disks" in basis["roles"] else "workdir",
                disks=list(disks.values()),
            )
        except (KeyError, ValueError, StopIteration) as e:
            raise HostInspectError(f"호스트 조회 결과를 해석하지 못했습니다: {e!r}") from e

        info["vagrant_version"] = values.get("VAGRANT") or None
        info["virtualbox_version"] = values.get("VBOX") or None
        # VirtualBox 에서 실제로 실행 중인 VM 전체(다른 사람 VM 포함)의 합계
        for out_key, key in (("vbox_running_vms", "VM_RUNNING"), ("vbox_vcpu_allocated", "VM_CPU_ALLOC"), ("vbox_mem_allocated_mb", "VM_MEM_ALLOC_MB")):
            info[out_key] = int(values[key]) if values.get(key, "").isdigit() else None
        return info
