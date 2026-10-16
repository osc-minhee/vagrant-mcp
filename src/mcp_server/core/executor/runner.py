# mcp_server/core/executor/runner.py
import asyncio
import logging
import os
import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from mcp_server.core.constants import SECRETS_DIR
from mcp_server.utils import get_env_int

logger = logging.getLogger(__name__)

@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str

class CommandRunner(ABC):
    """
    셸 스크립트를 실행할 위치(로컬, 원격 SSH 등)를 추상화한 클래스.
    """

    @abstractmethod
    async def run(self, script: str, timeout: int = 60) -> CommandResult:
        """
        bash 로 script 를 실행한다. script 안의 값은 호출하는 쪽에서 shlex.quote 로 감싸야 한다.
        """
        pass

class LocalRunner(CommandRunner):
    """
    MCP Server 가 돌고 있는 머신에서 직접 실행한다. (Vagrant 가 같은 머신에 설치된 경우)
    """

    async def run(self, script: str, timeout: int = 60) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            "bash", "-c", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError(f"명령이 {timeout}초 안에 끝나지 않았습니다.")

        return CommandResult(
            process.returncode,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace")
        )

class SSHRunner(CommandRunner):
    """
    Vagrant 가 설치된 원격 호스트에 SSH 로 접속해 실행한다.
    """

    def __init__(
        self,
        host: str,
        user: str,
        port: int = 22,
        key_path: Optional[str] = None,
        password: Optional[str] = None,
        connect_timeout: int = 10,
        strict_host_key: bool = False,
    ):
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path
        self.password = password
        self.connect_timeout = connect_timeout
        self.strict_host_key = strict_host_key

    async def run(self, script: str, timeout: int = 60) -> CommandResult:
        # paramiko 는 동기 라이브러리라 별도 스레드에서 돌린다.
        return await asyncio.to_thread(self._run_sync, script, timeout)

    def _run_sync(self, script: str, timeout: int) -> CommandResult:
        import paramiko

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if self.strict_host_key:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.user,
                password=self.password,
                key_filename=self.key_path,
                timeout=self.connect_timeout,
                banner_timeout=self.connect_timeout,
                auth_timeout=self.connect_timeout,
                look_for_keys=self.key_path is None and self.password is None,
            )
            # 원격 로그인 셸이 zsh 등일 수 있어 항상 bash 로 실행한다.
            # timeout 은 출력이 없는 상태로 기다리는 최대 시간이다.
            _, stdout, stderr = client.exec_command(f"bash -c {shlex.quote(script)}", timeout=timeout)
            out = stdout.read().decode(errors="replace")
            err = stderr.read().decode(errors="replace")
            return CommandResult(stdout.channel.recv_exit_status(), out, err)
        finally:
            client.close()

def build_runner(
    address: str,
    user: str = "vagrant",
    port: int = 22,
    key_path: Optional[str] = None,
    password: Optional[str] = None,
) -> CommandRunner:
    """
    address 가 비어 있거나 'local' 이면 LocalRunner, 아니면 SSHRunner.
    key_path 를 주지 않으면 SECRETS_DIR/.ssh/host_key 를 (있을 때) 쓴다.
    접속 제한 시간과 호스트 키 검증 여부는 모든 호스트가 같은 환경변수를 쓴다.
    """
    address = (address or "local").strip()
    if address == "local":
        logger.info("Vagrant 명령을 로컬에서 실행합니다.")
        return LocalRunner()

    key_path = key_path or os.getenv("VAGRANT_SSH_KEY") or str(SECRETS_DIR / ".ssh" / "host_key")
    if not os.path.exists(key_path):
        key_path = None

    logger.info(f"Vagrant 명령을 원격 호스트({address})에서 SSH 로 실행합니다.")
    return SSHRunner(
        host=address,
        user=user,
        port=port,
        key_path=key_path,
        password=password or os.getenv("VAGRANT_SSH_PASSWORD") or None,
        connect_timeout=get_env_int("VAGRANT_SSH_TIMEOUT", 10),
        strict_host_key=os.getenv("VAGRANT_SSH_STRICT_HOST_KEY", "false").lower() == "true",
    )

def sh_path(path: str) -> str:
    """
    셸에 넣을 경로를 quote 한다. '~/' 로 시작하면 $HOME 으로 풀리도록 둔다.
    """
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)
