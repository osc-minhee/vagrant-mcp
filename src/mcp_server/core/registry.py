# mcp_server/core/registry.py
import logging
from typing import Dict, Type, List
from mcp_server.core.base import ResourceProvisioner

logger = logging.getLogger(__name__)

class ProvisionerRegistry:
    """
    모든 Provisioner 구현체의 생명주기를 관리하는 클래스.
    """

    _registry: Dict[str, Dict[str, Type[ResourceProvisioner]]] = {}

    @classmethod
    def register(cls, provisioner_class: Type[ResourceProvisioner]):
        """
        Registry에 새로운 Provisioner 구현체를 등록한다.
        """
        provider = provisioner_class.provider_name()
        resource_type = provisioner_class.resource_type()

        if provider not in cls._registry:
            cls._registry[provider] = {}

        if resource_type in cls._registry[provider]:
            logger.warning(f"'{resource_type}' Provisioner가 이미 등록되어 있어 기존 Provisioner를 덮어씁니다.")

        cls._registry[provider][resource_type] = provisioner_class
        logger.info(f"등록 완료: [{provider}] {resource_type} Provisioner")

        return provisioner_class
    
    @classmethod
    def get_provisioner(cls, provider: str, resource_type: str) -> ResourceProvisioner:
        """
        Registry에서 리소스 타입에 맞는 Provisioner를 찾아 Instance를 반환한다.
        """
        try:
            p_class = cls._registry[provider][resource_type]
            return p_class()
        except KeyError:
            available = {p: list(res.keys()) for p, res in cls._registry.items()}
            raise RuntimeError(
                f"Provisioner를 찾을 수 없습니다: Provider='{provider}', Type='{resource_type}'\n"
                f"현재 등록된 Provisioner 목록: {available}"
            )
    
    @classmethod
    def list_available_types(cls) -> Dict[str, List[str]]:
        """
        현재 시스템에 로드된 모든 리소스 타입을 반환한다.
        """
        return {provider: list(resources.keys()) for provider, resources in cls._registry.items()}