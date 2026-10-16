# mcp_server/api/mcp/vagrant.py
from fastapi import APIRouter
import mcp_server.provisioners  # noqa: F401  (Provisioner 를 Registry 에 등록)
from mcp_server.core.constants import CloudProvider
from mcp_server.core.registry import ProvisionerRegistry
from mcp_server.schemas.vagrant import VagrantVMCreateInput, VagrantVMDestroyInput, VagrantVMUpdateInput

# Vagrant VM Tools
router = APIRouter(tags=["vagrant"])
vg_vm = ProvisionerRegistry.get_provisioner(CloudProvider.VAGRANT, "vm")

@router.post("/vg_create_vm", operation_id="vg_create_vm", description=(
    "Vagrant(VirtualBox)로 VM을 1개 생성하고 기초 환경을 구성하는 도구이다.\n"
    "생성은 백그라운드에서 진행되며 수 분이 걸린다. 진행 상태는 vg_list_vm 으로 확인한다.\n"
    "규칙:\n"
    "1. 필수는 name(소문자/숫자/하이픈, 전체에서 유일)뿐이다. 나머지는 사용자가 말하지 않았으면 되묻지 말고 비워서 바로 호출한다. 기본값: flavor=small, purpose=dev, project=호스트 기본 프로젝트.\n"
    "2. host 는 호스트가 1대면 비운다. 2대 이상이고 사용자가 말하지 않았으면 비워서 호출하고, 오류의 '사용 가능' 목록으로 한 번만 묻는다.\n"
    "3. 여러 대를 만들려면 반복 호출한다. 값을 지어내지 않는다.\n"
    "4. 호스트 자원이 부족하면 이유와 가능한 사양이 오류로 온다. 그대로 전하고 더 작은 사양이나 다른 호스트를 제안한다."
))
async def vg_create_vm(params: VagrantVMCreateInput):
    return await vg_vm.create(params)

@router.post("/vg_update_vm", operation_id="vg_update_vm", description=(
    "기존에 생성된 Vagrant VM의 설정(사양, 용도)을 변경하는 도구이다. 실행 중인 VM은 재시작(vagrant reload)된다.\n"
    "이 도구는 사용 시 다음 규칙을 준수해야 한다.\n"
    "1. 반드시 업데이트할 대상 VM의 정확한 name 파라미터를 포함해야 한다.\n"
    "2. flavor(사양)나 purpose(용도) 중 변경하려는 파라미터만 포함하여 전달한다. 빠진 값은 기존 값을 유지한다.\n"
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
    "ip_available(남은 IP 수)이 있고, 접속 실패는 reachable=false 와 error 로 온다."
))
async def vg_list_hosts():
    return await vg_vm.list_hosts()

@router.get("/vg_list_vm", operation_id="vg_list_vm", description=(
    "이 서버가 Vagrant로 생성한 전체 VM 목록(host, project 포함)과 상태(PENDING, PROVISIONING, CONFIGURING, RUNNING, ERROR, DESTROYING)를 조회하는 도구이다.\n"
    "ERROR 상태면 last_error 에 원인이 담겨 있다."
))
async def vg_list_vm():
    return await vg_vm.list_all()
