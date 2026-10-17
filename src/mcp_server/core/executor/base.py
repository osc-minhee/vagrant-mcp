# mcp_server/core/executor/base.py
from abc import ABC, abstractmethod
from typing import Any, Dict, Tuple

# (성공 여부, stdout, stderr)
ExecResult = Tuple[bool, str, str]

# (확인 통과 여부, 메시지). 통과 못 하면 메시지는 실패 원인, 통과했으면 경고(없으면 "")
VerifyResult = Tuple[bool, str]

class InfraExecutor(ABC):
    """
    인프라 리소스를 실제로 생성/변경/삭제하는 도구(Vagrant, Terraform 등)의 공통 인터페이스.
    ResourceProvisioner 는 이 인터페이스만 알고, 어떤 도구로 만드는지는 알지 못한다.
    """

    @abstractmethod
    async def apply(self, resource_id: str, vars: Dict[str, Any]) -> ExecResult:
        """
        vars 의 상태가 되도록 리소스를 생성하거나 변경한다. (여러 번 호출해도 같은 결과여야 한다)
        """
        pass

    @abstractmethod
    async def destroy(self, resource_id: str, vars: Dict[str, Any]) -> ExecResult:
        """
        리소스를 삭제한다. 이미 없는 리소스면 성공으로 본다.
        """
        pass

    async def verify_applied(self, resource_id: str, vars: Dict[str, Any]) -> VerifyResult:
        """
        apply 가 성공했다고 한 뒤, 리소스가 실제로 원하는 상태인지 확인한다. 기본은 확인하지 않는다.
        """
        return True, ""

    async def verify_destroyed(self, resource_id: str, vars: Dict[str, Any]) -> VerifyResult:
        """
        destroy 가 성공했다고 한 뒤, 리소스가 실제로 사라졌는지 확인한다. 기본은 확인하지 않는다.
        """
        return True, ""
