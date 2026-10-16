# mcp_server/core/executor/base.py
from abc import ABC, abstractmethod
from typing import Any, Dict, Tuple

# (성공 여부, stdout, stderr)
ExecResult = Tuple[bool, str, str]

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
