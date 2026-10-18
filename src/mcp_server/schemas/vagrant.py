# mcp_server/schemas/vagrant.py
from typing import Optional
from pydantic import BaseModel, Field
from mcp_server.core.constants import PROJECT_PATTERN, Flavor, Image

# VM 이름은 호스트의 폴더 이름으로도 쓰이므로 엄격하게 제한한다.
NAME_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$"
PURPOSE_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"
# IP 지정: 전체 IP(192.168.0.75) 또는 끝자리(75, *.*.*.75, x.x.x.75).
# 사용자 메시지의 IP 가 LLM 앞단에서 가려질 수 있어, 끝자리만으로도 호스트 IP 대역에서 찾을 수 있게 한다.
IP_SPEC_PATTERN = r"^(\d{1,3}(\.\d{1,3}){3}|\d{1,3}|([*xX]+\.){3}\d{1,3})$"
IP_SPEC_DESCRIPTION = (
    "전체 IP(예: 192.168.0.75) 또는 끝자리(예: 75). 끝자리를 주면 호스트 IP 대역에서 끝자리가 같은 IP 를 찾는다. "
    "사용자 메시지의 IP 가 가려져 보이면(마스킹) 지어내지 말고 사용자에게 끝자리를 물어본다."
)

class VagrantVMCreateInput(BaseModel):
    name: str = Field(..., pattern=NAME_PATTERN, description=(
        "VM 이름(소문자/숫자/하이픈, 1~32자, 예: sandbox-01)"
    ))
    flavor: Flavor = Field(Flavor.SMALL, description=(
        "VM 사양, 기본 small (small: 1 vCPU/2GiB, medium: 2 vCPU/4GiB, large: 4 vCPU/8GiB). 사용자가 말하지 않으면 묻지 않는다."
    ))
    cpu: Optional[int] = Field(None, ge=1, le=32, description=(
        "vCPU 수. 사용자가 vCPU 개수를 말했을 때만 넣고, 아니면 비운다(flavor 의 값). 메모리는 flavor 로 정한다."
    ))
    purpose: str = Field("dev", pattern=PURPOSE_PATTERN, description="VM 용도, 기본 dev. 사용자가 말하지 않으면 묻지 않는다.")
    image: Image = Field(Image.UBUNTU_2204, description=(
        "VM 이미지(OS), 기본 ubuntu-22.04 (ubuntu-22.04, rocky-8, rocky-9). 버전은 서버에서 고정한다. "
        "사용자가 말하지 않으면 묻지 않는다. 이미 만든 VM 의 이미지는 바꿀 수 없다."
    ))
    host: Optional[str] = Field(None, description=(
        "VM 을 만들 호스트 이름. 사용자가 호스트를 말했을 때만 넣는다. 비우면 ip 가 속한 호스트, 그다음 서버의 기본 호스트로 정해진다. "
        "이미 만든 VM 의 호스트는 바꿀 수 없다."
    ))
    project: Optional[str] = Field(None, pattern=PROJECT_PATTERN, description=(
        "VM 폴더를 넣을 프로젝트 폴더 이름(영문/숫자/._- 만, 한 단계). 사용자가 말하지 않으면 비운다(호스트 기본 프로젝트). "
        "이미 만든 VM 의 프로젝트는 바꿀 수 없다."
    ))
    ip: Optional[str] = Field(None, pattern=IP_SPEC_PATTERN, description=(
        "VM 고정 IP. 사용자가 특정 IP 를 말했을 때만 넣고, 아니면 비운다(호스트 IP 대역에서 자동 할당). "
        + IP_SPEC_DESCRIPTION + " 호스트의 IP 대역(ip_ranges) 안의 IP 만 쓸 수 있다. 이미 만든 VM 의 IP 는 바꿀 수 없다."
    ))

class VagrantVMUpdateInput(BaseModel):
    name: str = Field(..., pattern=NAME_PATTERN, description=(
        "변경할 VM 이름(예: sandbox-01)"
    ))
    flavor: Optional[Flavor] = Field(None, description=(
        "새 VM 사양(small, medium, large). 바꾸지 않으면 비운다."
    ))
    cpu: Optional[int] = Field(None, ge=1, le=32, description=(
        "새 vCPU 수. 바꾸지 않으면 비운다. flavor 를 함께 바꾸면 flavor 의 vCPU 대신 이 값을 쓴다."
    ))
    purpose: Optional[str] = Field(None, pattern=PURPOSE_PATTERN, description=(
        "새 VM 용도. 바꾸지 않으면 비운다."
    ))

class VagrantVMDestroyInput(BaseModel):
    name: str = Field(..., pattern=NAME_PATTERN, description=(
        "VM 이름(예: sandbox-01)"
    ))
