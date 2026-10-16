# mcp_server/core/executor/vagrant.py
import asyncio
import base64
import logging
from pathlib import Path
from typing import Any, Dict, List

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from mcp_server.core.executor.base import ExecResult, InfraExecutor
from mcp_server.core.executor.runner import CommandRunner, sh_path

# 리소스마다 {workdir}/{resource_id} 폴더를 만들고 그 안에 Vagrantfile 을 둔다.
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

class VagrantExecutor(InfraExecutor):
    """
    Vagrantfile 을 렌더링해 원격(또는 로컬) 호스트에서 vagrant 명령을 실행하는 클래스.
    """
    def __init__(
        self,
        runner: CommandRunner,
        workdir: str,
        template_path: Path,
        ssh_pubkey_paths: List[Path],
        timeout: int = 1800,
        max_parallel: int = 2,
    ):
        self.runner = runner
        self.workdir = workdir.rstrip("/") or "/"
        self.template_path = template_path
        self.ssh_pubkey_paths = ssh_pubkey_paths
        self.timeout = timeout
        # 동시에 너무 많은 VM 을 올리면 box 다운로드/VirtualBox 가 충돌하므로 동시 실행 수를 제한한다.
        self.semaphore = asyncio.Semaphore(max_parallel)
        self.logger = logging.getLogger(__name__)

        self.jinja = Environment(
            loader=FileSystemLoader(str(template_path.parent)),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def _vm_dir(self, resource_id: str) -> str:
        return f"{self.workdir}/{resource_id}"

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

    async def _run(self, resource_id: str, command: str, script: str) -> ExecResult:
        async with self.semaphore:
            self.logger.info(f"[{resource_id}] vagrant {command} 실행 중...")
            result = await self.runner.run(script, timeout=self.timeout)

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
        vagrantfile = self.render(resource_id, vars)
        script = (
            _APPLY_SCRIPT
            .replace("__DIR__", sh_path(self._vm_dir(resource_id)))
            .replace("__VF_B64__", base64.b64encode(vagrantfile.encode()).decode())
        )
        return await self._run(resource_id, "up/reload", script)

    async def destroy(self, resource_id: str, vars: Dict[str, Any]) -> ExecResult:
        script = _DESTROY_SCRIPT.replace("__DIR__", sh_path(self._vm_dir(resource_id)))
        return await self._run(resource_id, "destroy", script)

    async def is_ip_in_use(self, ip: str) -> bool:
        """
        Vagrant 호스트에서 ping 으로 IP 사용 여부를 확인한다. (DB 밖에서 쓰고 있는 IP 를 피하기 위함)
        """
        result = await self.runner.run(f"ping -c1 -W1 {ip} >/dev/null 2>&1", timeout=10)
        return result.exit_code == 0
