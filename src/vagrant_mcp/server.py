"""vagrant-mcp 서버 진입점 (mcp 2.x: FastMCP -> MCPServer)."""

import functools
import inspect
from typing import Literal

import anyio.to_thread
import paramiko
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .config import get_settings
from .create import (
    IMAGES,
    MEMORY_CHOICES_MB,
    CreateResult,
    HostCreateOptions,
    create_options,
    create_vm,
)
from .delete import DeleteCheck, DeleteResult, delete_check, destroy, take_token
from .hosts import HostInfo, list_hosts
from .redact import redact, redact_text
from .vms import HostVms, list_vms

mcp = MCPServer("vagrant-mcp")


def _to_tool_error(e: Exception) -> ToolError:
    if isinstance(e, (OSError, paramiko.SSHException)):
        return ToolError(redact_text(f"SSH 접속/실행 실패 ({type(e).__name__}: {e})"))
    return ToolError(redact_text(str(e)))


# 메시지를 LLM 에 보여줄 예외: 일부러 낸 안내(ValueError/RuntimeError) + SSH/네트워크 오류
_SHOWN_ERRORS = (ValueError, RuntimeError, OSError, paramiko.SSHException)


def tool():
    """mcp.tool() + 위 예외들을 ToolError 로 바꾸고, 결과/에러 메시지의 민감한 값을 가린다(redact).

    MCPServer 는 ToolError 가 아닌 예외의 메시지를 숨기고 "Error executing tool <name>" 만 보낸다.
    이 코드의 ValueError/RuntimeError 는 일부러 낸 안내(IP 부족, 메모리 상한 등)라 LLM 이 읽어야 하고,
    SSH 접속 실패도 원인을 알려줘야 한다.
    """

    def decorator(fn):
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                try:
                    return redact(await fn(*args, **kwargs))
                except _SHOWN_ERRORS as e:
                    raise _to_tool_error(e) from e

        else:

            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                try:
                    return redact(fn(*args, **kwargs))
                except _SHOWN_ERRORS as e:
                    raise _to_tool_error(e) from e

        return mcp.tool()(wrapper)

    return decorator


@tool()
def host_list() -> list[HostInfo]:
    """VM 을 생성할 수 있는 호스트 목록과 각 호스트의 가용 자원을 조회한다.

    메모리/디스크는 *_mb(MiB) 와 *_gi(GiB, free -h 와 같은 단위)를 함께 준다. 사용자에게는 *_gi 로 보여준다.
    mem_allocated_gi 가 mem_total_gi 보다 크면 VM 에 실제 메모리보다 많이 할당된(오버커밋) 상태라고 알린다.
    cpu_free_estimate 는 부하 기반 추정치이며,
    접속에 실패한 호스트는 reachable=false 와 error 메시지가 담겨 돌아온다.
    vm_running_count / vcpu_allocated / mem_allocated_mb 는 VirtualBox 에서 실제로
    실행 중인 VM 전체(다른 사람 VM 포함)에 할당된 합계이다. VirtualBox 가 없으면 null.
    VM 생성 전에 사용자에게 어떤 호스트에 만들지 물어볼 때 이 결과를 보여준다.
    """
    return list_hosts()


@tool()
def vm_list(host: str | None = None, managed_only: bool = False) -> list[HostVms]:
    """호스트별 Vagrant VM 목록과 상태를 조회한다. (읽기 전용)

    host 에 호스트 이름(예: host-a)을 주면 그 호스트만 조회하고, 비우면 전체를 조회한다.
    기본은 서버에 있는 모든 사람의 VM 을 보여준다. managed=true 인 VM 이 이 에이전트(vm_create)가
    만든 VM 이고, managed_only=true 로 주면 그것만 보여준다.
    각 VM 은 name(폴더 이름), project(상위 프로젝트 폴더), status(running/poweroff/
    aborted/saved/not_created=만든 적 없음/missing=기록은 있는데 VirtualBox 에 없음),
    ip, cpu, memory_mb(MiB)/memory_gi(GiB, 사용자에게는 이걸 보여준다), box 를 가진다. 상태는 VirtualBox 에서 실시간으로 읽는다.
    unmatched_running 은 실행 중인데 어느 폴더와도 연결되지 않은 VM 으로, 자원은 쓰고 있다.
    새 VM 을 만들기 전에 이름이 이미 있는지 확인할 때도 이 도구를 쓴다.
    IP/호스트 주소는 x.x.x.x / <host_address> 로, 경로의 workdir 는 <workdir> 로 가려질 수 있다. 가려진 값을 추측하지 않는다.
    """
    return list_vms(host, managed_only)


@tool()
def vm_create_options(host: str | None = None) -> list[HostCreateOptions]:
    """VM 생성 전에 호스트별로 만들 수 있는 조건을 조회한다. (읽기 전용)

    VM 을 만들기 전에 반드시 먼저 호출해서 사용자에게 보여주고 아래 값을 물어본다.
      - host      : 어느 호스트에 만들지 (예: host-a, host-b) - 사용자가 고른다
      - name      : VM 이름 (필수. 소문자/숫자/하이픈)
      - project   : 넣을 프로젝트 폴더 (projects 중 하나, 예: k8s-test). 비우면 default_project
                    (default_project 가 null 이면 workdir 바로 아래)
      - ip        : available_ips 중 하나. 비우면 자동 할당. available_ips 가 비어 있고
                    available_ip_count > 0 이면 IP 가 가려진 것이므로 묻지 말고 비워서 자동 할당한다
      - cpu       : 비우면 defaults.cpu. 제한 없음
      - memory_mb : memory_choices 중 allowed=true 인 값만 고를 수 있다. 기본값은 16384 (16Gi)
      - image     : images 중 하나. 사용자가 고른다 (버전은 서버가 고정)
    image 와 memory_mb 는 자유 입력으로 받지 말고 선택지로 보여준다. 클라이언트에 선택 UI(버튼/선택 목록)가
    있으면 그걸 쓴다. 메모리 선택지는 label 을 그대로 쓰고, default=true 인 값은 **굵게** 표시하고
    "(기본값)" 이라고 적는다. allowed=false 인 값은 상한 초과라서 고를 수 없다고 표시하거나 뺀다.
    existing 에 있는 위치("이름" 또는 "프로젝트/이름")에는 만들 수 없다.
    사용자에게 메모리 상한(limits.max_memory_gi, GiB)을 반드시 알려준다. 메모리/디스크 수치는 *_gi 로 보여준다. 기본 메모리가 상한보다 크거나
    limits.disk_ok=false 면 그 사실도 알린다.
    IP 는 모든 호스트가 같은 망을 쓰므로 모든 호스트의 VM 을 기준으로 사용 중 여부를 계산한다.
    available_ip_count 가 0 이면 그 호스트에는 남은 IP 가 없다.
    """
    return create_options(host)


_PROGRESS_INTERVAL_SEC = 15

ImageName = Literal[tuple(IMAGES)]  # type: ignore[valid-type]
MemoryMb = Literal[MEMORY_CHOICES_MB]  # type: ignore[valid-type]


@tool()
async def vm_create(
    host: str,
    name: str,
    image: ImageName,
    memory_mb: MemoryMb,
    ctx: Context,
    ip: str | None = None,
    cpu: int | None = None,
    project: str | None = None,
    owner: str | None = None,
) -> CreateResult:
    """새 Vagrant VM 을 만든다. 호출 전에 vm_create_options 결과를 보여주고 사용자 확인을 받는다.

    템플릿 폴더를 {workdir}/{project}/{name} 으로 복사하고 (project 를 비우면 호스트의 default_project,
    그것도 없으면 {workdir}/{name})
    Vagrantfile 의 이름/IP/CPU/메모리/이미지만 바꾼 뒤 `vagrant up` 이 끝날 때까지 기다린다.
    수 분(box 를 처음 받으면 10분 이상) 걸린다. 사용자에게 오래 걸린다고 미리 알려준다.
    status: running(성공) / failed(실패 - output_tail 을 보여준다) / unknown(결과를 못 받음 - vm_list 로 확인).
    image   : 사용자가 vm_create_options 의 images 중에서 고른 이미지. 지어내지 않는다.
    memory_mb: 사용자가 memory_choices(allowed=true) 중에서 고른 값. 사용자가 기본값(16384)으로 하겠다고
               하지 않았다면 임의로 채우지 말고 물어본다. 상한을 넘으면 에러가 난다.
    ip/cpu 를 비우면 자동 할당 / 템플릿 기본값을 쓴다.
    owner 는 VM 을 쓸 사람 이름(사용자에게 물어서 받는다). 주면 폴더에 .owner.json 으로 남고,
    vm_delete_check 때 요청자와 다르면 "현재 owner 는 ~ 입니다" 로 알려준다 (막지는 않음).
    """

    async def heartbeat() -> None:
        # 기다리는 동안 진행 알림을 보내서, 진행 알림을 받으면 타임아웃을 늘려주는 클라이언트가 끊지 않게 한다
        elapsed = 0
        while True:
            await anyio.sleep(_PROGRESS_INTERVAL_SEC)
            elapsed += _PROGRESS_INTERVAL_SEC
            try:
                await ctx.report_progress(elapsed, None, f"VM '{name}' 생성 중 ({elapsed}초 경과)")
            except Exception:
                pass

    # 예외를 task group 밖으로 그대로 던지면 anyio 가 ExceptionGroup 으로 감싸서 에러 메시지가 사라진다.
    # 안에서 받아 두었다가 밖에서 원래 예외로 다시 던진다.
    error: Exception | None = None
    async with anyio.create_task_group() as tg:
        tg.start_soon(heartbeat)
        try:
            result = await anyio.to_thread.run_sync(
                lambda: create_vm(host, name, ip, cpu, memory_mb, image, project, owner)
            )
        except Exception as e:
            error = e
        finally:
            tg.cancel_scope.cancel()
    if error is not None:
        raise error
    return result


@tool()
def vm_delete_check(
    host: str, name: str, project: str | None = None, requested_by: str | None = None
) -> DeleteCheck:
    """VM 을 지우기 전에 누가 쓰고 있는지 점검한다. (VM 은 바꾸지 않음) 수십 초 걸릴 수 있다.

    host/project/name 은 vm_list 결과의 값을 그대로 준다. requested_by 는 삭제를 요청한 사람 이름
    (사용자에게 물어서 받는다. 지어내지 않는다). VM 에 소유자 기록(owner)이 있고 요청자와 다르면
    막지 않고 reasons 에 "현재 owner 는 ~ 입니다" 로 알린다.
    실행 중인 머신은 VM 안에서 최근 window_days 일 로그인 수(현재 접속 포함)와 수정 파일 수,
    실행 중인 docker 컨테이너 수, k8s 흔적(6443/10250/9345 포트, k8s 설정 폴더), 열린 서비스 포트 수(22, 로컬 전용 제외)를 본다.
    verdict:
      - ok      : 사용 흔적 없음
      - caution : owner 가 요청자와 다름/요청자 미지정, 에이전트가 만든 VM 이 아님(마커 없음), 최근 로그인/수정 파일,
                  docker/k8s, 열린 서비스 포트, 꺼져 있어 안을 점검할 수 없음, VM 안 점검 실패 중 하나라도 있음.
                  reasons 를 사용자에게 그대로 보여주고(특히 owner 와 마커 경고) 그래도 지울지 묻는다
    confirm_token(30분, 1회용)을 vm_delete 에 넘긴다.
    """
    return delete_check(host, name, project, requested_by)


@tool()
async def vm_delete(
    host: str,
    name: str,
    confirm_token: str,
    confirm_name: str,
    project: str | None = None,
) -> DeleteResult:
    """VM 을 영구 삭제한다 (vagrant destroy -f + 폴더 삭제). 되돌릴 수 없다.

    반드시 vm_delete_check 결과(verdict, reasons, owner)를 사용자에게 보여주고 삭제 동의를 받은 뒤 호출한다.
    confirm_token 은 vm_delete_check 가 준 값. host/name/project 는 점검 때와 같게 준다.
    confirm_name 은 사용자에게 "삭제하려면 VM 이름을 그대로 입력하세요" 라고 요청해서 사용자가 채팅에 직접
    입력한 값을 그대로 준다. 절대 대신 채우지 않는다. name 과 다르면 삭제하지 않는다 (토큰은 그대로 남는다).
    삭제 직전에 대상 경로만 다시 확인한다.
    """
    if confirm_name.strip() != name:
        raise ValueError(
            f"입력한 이름({confirm_name!r})이 삭제 대상({name!r})과 달라 삭제하지 않았습니다. 사용자에게 다시 입력받으세요."
        )
    pending = take_token(confirm_token, host, name, project)

    return await anyio.to_thread.run_sync(
        lambda: destroy(pending, host, name, project, "user_typed_name")
    )


def main() -> None:
    s = get_settings()
    mcp.run("streamable-http", host=s.server_host, port=s.server_port)


if __name__ == "__main__":
    main()