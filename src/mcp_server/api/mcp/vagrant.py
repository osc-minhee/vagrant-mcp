# mcp_server/api/mcp/vagrant.py
from typing import Optional
from fastapi import APIRouter, Query
import mcp_server.provisioners  # noqa: F401  (Provisioner 를 Registry 에 등록)
from mcp_server.core.constants import CloudProvider, ResourceState
from mcp_server.core.registry import ProvisionerRegistry
from mcp_server.utils import now_local
from mcp_server.schemas.vagrant import (
    IP_SPEC_DESCRIPTION, IP_SPEC_PATTERN, NAME_PATTERN, VagrantVMCreateInput, VagrantVMDestroyInput, VagrantVMUpdateInput,
)

# Vagrant VM Tools
router = APIRouter(tags=["vagrant"])
vg_vm = ProvisionerRegistry.get_provisioner(CloudProvider.VAGRANT, "vm")

@router.post("/vg_create_vm", operation_id="vg_create_vm", description=(
    "Vagrant(VirtualBox)로 VM을 1개 생성하고 기초 환경을 구성하는 도구이다.\n"
    "생성은 백그라운드에서 진행되며 수 분이 걸린다. 진행 상태는 vg_get_vm 또는 vg_list_vm 을 다시 호출해 확인한다 (이 도구의 응답은 시작 시점의 정보다).\n"
    "규칙:\n"
    "1. 필수는 name(소문자/숫자/하이픈, 전체에서 유일)뿐이다. 나머지는 사용자가 말하지 않았으면 되묻지 말고 비워서 바로 호출한다. 기본값: flavor=small, purpose=dev, image=ubuntu-22.04, project=호스트 기본 프로젝트.\n"
    "   image 는 ubuntu-22.04, rocky-8, rocky-9 중 하나다 (사용자가 Rocky/CentOS 계열을 원하면 rocky-9).\n"
    "   cpu 는 사용자가 vCPU 개수를 말했을 때만 넣는다 (flavor 의 vCPU 대신 쓰이고, 메모리는 flavor 로 정해진다).\n"
    "   dir_name 은 사용자가 VM 폴더 이름을 말했을 때만 넣는다 (비우면 vg-<name>). 이미 있는 폴더면 이유가 오류로 오니 다른 이름을 받는다.\n"
    "2. host 는 사용자가 호스트를 말했을 때만 넣는다. 비우면 ip 가 속한 호스트, 그다음 서버의 기본 호스트에 만든다. "
    "기본 호스트가 없어 오류가 오면 그 '사용 가능' 목록으로 한 번만 묻는다.\n"
    "3. 여러 대를 만들려면 반복 호출한다. 값을 지어내지 않는다.\n"
    "4. 호스트 자원이 부족하면 이유와 가능한 사양이 오류로 온다. 그대로 전하고 더 작은 사양이나 다른 호스트를 제안한다.\n"
    "5. ip 는 사용자가 특정 IP 를 말했을 때만 넣는다. 전체 IP 또는 끝자리(예: 75)를 넣을 수 있다. "
    "사용자 메시지의 IP 가 가려져 보이면(마스킹) 지어내지 말고 끝자리를 물어본다. "
    "호스트 IP 대역 안의 IP 만 쓸 수 있고, 대역 밖이거나 사용 중이면 이유(와 대역)가 오류로 온다. "
    "그대로 전하고 다른 IP 를 받거나 비워서(자동 할당) 다시 호출한다. 미리 확인하려면 vg_check_ip 를 쓴다."
))
async def vg_create_vm(params: VagrantVMCreateInput):
    return await vg_vm.create(params)

@router.get("/vg_check_ip", operation_id="vg_check_ip", description=(
    "VM 에 줄 IP 를 쓸 수 있는지 확인하는 도구이다. (읽기 전용, 생성하지 않는다. 호스트에 SSH 로 확인하므로 수 초 걸린다)\n"
    "확인 기준: 이 서버가 만든 VM, 호스트의 Vagrantfile 에 적힌 IP, 호스트에서 실행 중인 VirtualBox VM. 네트워크의 다른 장비는 확인하지 않는다.\n"
    "ip 를 주면 그 IP 하나의 available 과 reason(못 쓰는 이유)을, 비우면 호스트의 IP 대역(ip_ranges)과 "
    "비어 있는 구간(available_ranges), 개수(available_count)를 돌려준다.\n"
    "사용자가 'IP 가 비어 있는지', '어느 호스트에서 쓸 수 있는 IP 대역'을 물을 때 쓴다. 확인 뒤 다른 요청이 먼저 가져갈 수 있어 생성 때 한 번 더 확인된다."
))
async def vg_check_ip(
    ip: Optional[str] = Query(None, pattern=IP_SPEC_PATTERN, description="확인할 IP. " + IP_SPEC_DESCRIPTION + " 비우면 비어 있는 IP 목록."),
    host: Optional[str] = Query(None, description="호스트 이름. 사용자가 말하지 않았으면 비운다(ip 가 속한 호스트, 그다음 기본 호스트)."),
):
    return await vg_vm.check_ip(host=host, ip=ip)

@router.post("/vg_update_vm", operation_id="vg_update_vm", description=(
    "기존에 생성된 Vagrant VM의 설정(사양, vCPU, 용도)을 변경하는 도구이다. 실행 중인 VM은 재시작(vagrant reload)된다.\n"
    "이 도구는 사용 시 다음 규칙을 준수해야 한다.\n"
    "1. 반드시 업데이트할 대상 VM의 정확한 name 파라미터를 포함해야 한다.\n"
    "2. flavor(사양), cpu(vCPU 수), purpose(용도) 중 변경하려는 파라미터만 포함하여 전달한다. 빠진 값은 기존 값을 유지한다.\n"
    "3. VM이 재시작되므로 호출 전에 사용자에게 확인을 받는다."
))
async def vg_update_vm(params: VagrantVMUpdateInput):
    return await vg_vm.update(params)

@router.post("/vg_destroy_vm", operation_id="vg_destroy_vm", description=(
    "특정 이름의 Vagrant VM을 영구 삭제하는 도구이다(vagrant destroy + 폴더 삭제). 되돌릴 수 없다.\n"
    "반드시 사용자에게 삭제할 VM 이름을 보여주고 확인을 받은 뒤 호출한다."
))
async def vg_destroy_vm(params: VagrantVMDestroyInput):
    return await vg_vm.destroy(params)

@router.get("/vg_list_hosts", operation_id="vg_list_hosts", description=(
    "Vagrant 호스트 목록과 각 호스트의 자원 현황을 조회하는 도구이다. (읽기 전용, SSH 로 조회하므로 수 초 걸린다)\n"
    "사용자가 호스트 상태를 묻거나 어느 호스트에 만들지 고를 때만 호출한다. VM 생성 전에 매번 호출하지 않는다.\n"
    "호스트마다 CPU, 메모리/디스크(사용자에게는 *_gi 로 보여준다), vbox_*(실행 중인 VM 전체 합계), managed_vms(이 서버가 만든 VM 수), "
    "ip_available(남은 IP 수), ip_used_by_others(대역 안을 이 서버가 만들지 않은 VM 이 쓰는 수)가 있고, 접속 실패는 reachable=false 와 error 로 온다.\n"
    "호스트에 있는 VM 하나하나를 보려면 vg_list_host_vms 를 쓴다."
))
async def vg_list_hosts():
    return await vg_vm.list_hosts()

@router.get("/vg_list_host_vms", operation_id="vg_list_host_vms", description=(
    "호스트에 있는 VM 전체(이 서버가 만들지 않은 VM, 다른 계정의 VM 포함)를 조회하는 도구이다. (읽기 전용, SSH 로 조회하므로 수 초 걸린다)\n"
    "호스트 scan_root 아래의 Vagrantfile(project_dir, vagrant_machine), 실행 중인 VirtualBox 프로세스(owner), VirtualBox 등록 정보를 합친 결과다.\n"
    "owner 는 실제 계정명이 아닌 별칭이다(server-account: 이 서버의 계정, user-1/user-2...: 다른 계정). 같은 별칭은 같은 계정이다. 실제 계정명을 추측하거나 묻지 않는다.\n"
    "state: running, poweroff/saved/aborted(이 서버 계정의 VM 만 구분됨), not_running(꺼져 있음. 다른 계정 VM 이라 세부 상태는 모름), "
    "not_created(Vagrantfile 만 있고 vagrant up 한 적 없음), unknown(.vagrant 기록을 읽을 권한이 없음).\n"
    "managed=true 는 이 서버가 만든 VM 이고 managed_name 이 vg_* 도구에서 쓰는 이름이다. managed=false 인 VM 은 이 서버로 변경/삭제할 수 없다.\n"
    "host 는 사용자가 호스트를 말했을 때만 넣고, 비우면 서버의 기본 호스트를 조회한다. 모든 호스트를 보려면 호스트마다 호출한다. "
    "notes 가 있으면 조회하지 못한 부분이니 함께 전한다."
))
async def vg_list_host_vms(
    host: Optional[str] = Query(None, description="조회할 호스트 이름. 사용자가 말하지 않았으면 비운다(기본 호스트)."),
):
    return await vg_vm.list_host_vms(host)

@router.get("/vg_list_vm", operation_id="vg_list_vm", description=(
    "이 서버가 Vagrant로 생성한 전체 VM 목록(host, project, image 포함)과 상태(PENDING, PROVISIONING, CONFIGURING, RUNNING, ERROR, DESTROYING)를 조회하는 도구이다.\n"
    "ERROR 상태면 last_error 에 원인이 담겨 있다. 사용자가 호스트나 상태를 말했을 때만 host, state 로 거른다.\n"
    "이 서버의 기록 기준이므로, VM 이 실제로 떠 있는지는 vg_get_vm 으로 확인한다.\n"
    "상태는 백그라운드 작업으로 계속 바뀐다. 사용자가 목록이나 상태를 물을 때마다 반드시 다시 호출하고, 앞서 받은 결과를 재사용하지 않는다. "
    "checked_at 이 조회 시각이다."
))
async def vg_list_vm(
    host: Optional[str] = Query(None, description="이 호스트의 VM 만 보려면 호스트 이름. 아니면 비운다."),
    state: Optional[ResourceState] = Query(None, description="이 상태의 VM 만 보려면 상태. 아니면 비운다."),
):
    vms = await vg_vm.list_all(host=host, state=state)
    return {"checked_at": now_local(), "count": len(vms), "vms": vms}

@router.get("/vg_get_vm", operation_id="vg_get_vm", description=(
    "VM 1대의 기록(vg_list_vm 과 같은 항목)과 호스트의 VirtualBox 에서 확인한 실제 상태(actual)를 조회하는 도구이다. (읽기 전용, SSH 로 확인하므로 수 초 걸린다)\n"
    "사용자가 특정 VM 하나를 묻거나, VM 이 실제로 떠 있는지 확인할 때 쓴다.\n"
    "mismatch 가 있으면 기록과 실제가 다른 것이니 그대로 사용자에게 알린다. actual.checked=false 면 호스트에 접속하지 못한 것이다(error 참고).\n"
    "상태를 물을 때마다 반드시 다시 호출하고, 앞서 받은 결과를 재사용하지 않는다. checked_at 이 조회 시각이다."
))
async def vg_get_vm(
    name: str = Query(..., pattern=NAME_PATTERN, description="조회할 VM 이름(예: sandbox-01)"),
):
    return await vg_vm.get(name)
