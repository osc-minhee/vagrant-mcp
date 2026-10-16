# mcp_server/schemas/vagrant.py
from typing import Optional
from pydantic import BaseModel, Field
from mcp_server.core.constants import Flavor

# VM 이름은 호스트의 폴더 이름으로도 쓰이므로 엄격하게 제한한다.
NAME_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$"
PURPOSE_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"

class VagrantVMCreateInput(BaseModel):
    name: str = Field(..., pattern=NAME_PATTERN, description=(
        "VM 이름(소문자/숫자/하이픈, 1~32자, 예: sandbox-01)"
    ))
    flavor: Flavor = Field(Flavor.SMALL, description=(
        "VM 사양(small: 1 vCPU/2GiB, medium: 2 vCPU/4GiB, large: 4 vCPU/8GiB)"
    ))
    purpose: str = Field("dev", pattern=PURPOSE_PATTERN, description="VM 용도(예: dev, sandbox)")

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
