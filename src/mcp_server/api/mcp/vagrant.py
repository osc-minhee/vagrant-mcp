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
    "이 도구는 사용 시 다음 규칙을 준수해야 한다.\n"
    "1. 여러 대의 VM을 생성해야 할 경우에는, 이 도구를 반복해서 호출해야 한다.\n"
    "2. name 파라미터는 반드시 소문자, 숫자와 하이픈(-)만 사용해야 한다."
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

@router.get("/vg_list_vm", operation_id="vg_list_vm", description=(
    "이 서버가 Vagrant로 생성한 전체 VM 목록과 상태(PENDING, PROVISIONING, CONFIGURING, RUNNING, ERROR, DESTROYING)를 조회하는 도구이다.\n"
    "ERROR 상태면 last_error 에 원인이 담겨 있다."
))
async def vg_list_vm():
    return await vg_vm.list_all()
