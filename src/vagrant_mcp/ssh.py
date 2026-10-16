"""paramiko 기반 원격 명령 실행."""

import shlex
from dataclasses import dataclass

import paramiko

from .config import HostConfig, get_settings


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


def run_command(host: HostConfig, command: str, timeout: int = 30) -> CommandResult:
    """host 에 SSH 로 접속해 command 를 실행하고 결과를 돌려준다.

    command 는 호출하는 쪽에서 shlex.quote 등으로 안전하게 만들어서 넘길 것.
    """
    settings = get_settings()

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if settings.ssh_strict_host_key:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            hostname=host.address,
            port=host.port,
            username=host.user,
            password=settings.ssh_password,
            key_filename=settings.ssh_key_path,
            timeout=settings.ssh_timeout_sec,
            banner_timeout=settings.ssh_timeout_sec,
            auth_timeout=settings.ssh_timeout_sec,
            look_for_keys=settings.ssh_key_path is None and settings.ssh_password is None,
        )
        # 원격 로그인 셸이 zsh 등일 수 있어서 항상 bash 로 실행한다.
        # (zsh 는 글롭이 안 맞으면 에러로 멈추는 등 bash 와 동작이 다르다)
        _, stdout, stderr = client.exec_command(f"bash -c {shlex.quote(command)}", timeout=timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        return CommandResult(stdout.channel.recv_exit_status(), out, err)
    finally:
        client.close()