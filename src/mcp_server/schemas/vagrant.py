# mcp_server/schemas/vagrant.py
from typing import Optional
from pydantic import BaseModel, Field
from mcp_server.core.constants import PROJECT_PATTERN, Flavor

# VM 이름은 호스트의 폴더 이름으로도 쓰이므로 엄격하게 제한한다.
NAME_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$"
PURPOSE_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"

class VagrantVMCreateInput(BaseModel):
    name: str = Field(..., pattern=NAME_PATTERN, description=(
        "VM 이름(소문자/숫자/하이픈, 1~32자, 예: sandbox-01)"
    ))
    flavor: Flavor = Field(Flavor.SMALL, description=(
        "VM 사양, 기본 small (small: 1 vCPU/2GiB, medium: 2 vCPU/4GiB, large: 4 vCPU/8GiB). 사용자가 말하지 않으면 묻지 않는다."
    ))
    purpose: str = Field("dev", pattern=PURPOSE_PATTERN, description="VM 용도, 기본 dev. 사용자가 말하지 않으면 묻지 않는다.")
    host: Optional[str] = Field(None, description=(
        "VM 을 만들 호스트 이름. 호스트가 1대면 비운다. 이미 만든 VM 의 호스트는 바꿀 수 없다."
    ))
    project: Optional[str] = Field(None, pattern=PROJECT_PATTERN, description=(
        "VM 폴더를 넣을 프로젝트 폴더 이름(영문/숫자/._- 만, 한 단계). 사용자가 말하지 않으면 비운다(호스트 기본 프로젝트). "
        "이미 만든 VM 의 프로젝트는 바꿀 수 없다."
    ))

class VagrantVMUpdateInput(BaseModel):
    name: str = Field(..., pattern=NAME_PATTERN, description=(
        "변경할 VM 이름(예: sandbox-01)"
    ))
    flavor: Optional[Flavor] = Field(None, description=(
        "새 VM 사양(small, medium, large). 바꾸지 않으면 비운다."
    ))
    purpose: Optional[str] = Field(None, pattern=PURPOSE_PATTERN, description=(
        "새 VM 용도. 바꾸지 않으면 비운다."
    ))

class VagrantVMDestroyInput(BaseModel):
    name: str = Field(..., pattern=NAME_PATTERN, description=(
        "VM 이름(예: sandbox-01)"
    ))
