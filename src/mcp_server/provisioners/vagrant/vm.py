# mcp_server/provisioners/vagrant/vm.py
import ipaddress
import json
import os
from typing import Any, Dict, List, Optional
from mcp_server.core.base import ResourceProvisioner
from mcp_server.core.constants import APP_DIR, SECRETS_DIR, CloudProvider, Flavor
from mcp_server.core.executor.ansible import AnsibleExecutor
from mcp_server.core.executor.runner import build_runner_from_env
from mcp_server.core.executor.vagrant import VagrantExecutor
from mcp_server.core.registry import ProvisionerRegistry
from mcp_server.database import get_used_values
from mcp_server.schemas.base import ProvisioningManifest
from mcp_server.schemas.vagrant import VagrantVMCreateInput
from mcp_server.utils import get_env_int

FLAVOR_SPECS = {
    Flavor.SMALL: {"cpu": 1, "memory": 2048},
    Flavor.MEDIUM: {"cpu": 2, "memory": 4096},
    Flavor.LARGE: {"cpu": 4, "memory": 8192},
}

def expand_ip_range(spec: str) -> List[str]:
    """
    "10.0.0.60-89" / "10.0.0.60-10.0.0.89" / "10.0.0.60" 을 IP 목록으로 푼다. 여러 대역은 쉼표로 구분한다.
    """
    ips: List[str] = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        start, _, end = part.partition("-")
        first = ipaddress.IPv4Address(start.strip())
        end = end.strip()
        if not end:
            last = first
        elif "." in end:
            last = ipaddress.IPv4Address(end)
        else:
            last = ipaddress.IPv4Address(f"{start.strip().rsplit('.', 1)[0]}.{end}")
        if last < first:
            raise ValueError(f"IP 대역의 끝이 시작보다 작습니다: {part}")
        ips.extend(str(ipaddress.IPv4Address(i)) for i in range(int(first), int(last) + 1))
    return list(dict.fromkeys(ips))

IP_POOL = expand_ip_range(os.getenv("VAGRANT_IP_RANGE", ""))
SSH_DIR = SECRETS_DIR / ".ssh"

vagrant_executor = VagrantExecutor(
    runner=build_runner_from_env(),
    workdir=os.getenv("VAGRANT_WORKDIR", "~/vagrant-mcp"),
    template_path=APP_DIR / "vagrant" / "templates" / "Vagrantfile.j2",
    ssh_pubkey_paths=[SSH_DIR / "agent_key.pub", SSH_DIR / "admin_key.pub"],
    timeout=get_env_int("VAGRANT_TIMEOUT", 1800),
    max_parallel=get_env_int("VAGRANT_MAX_PARALLEL", 2),
)

ansible_executor = AnsibleExecutor(
    ansible_dir=str(APP_DIR / "ansible"),
    default_playbook="base.yml",
    remote_user="vagrant",
    private_key=str(SSH_DIR / "agent_key"),
)

@ProvisionerRegistry.register
class VagrantVMProvisioner(ResourceProvisioner):
    @property
    def infra_executor(self):
        return vagrant_executor

    @property
    def config_executor(self):
        # VAGRANT_SKIP_ANSIBLE=true 면 Vagrant 로 VM 만 만들고 끝낸다.
        if os.getenv("VAGRANT_SKIP_ANSIBLE", "false").lower() == "true":
            return None
        return ansible_executor

    @classmethod
    def provider_name(cls):
        return CloudProvider.VAGRANT

    @classmethod
    def resource_type(cls):
        return "vm"

    async def _allocate_ip(self) -> str:
        if not IP_POOL:
            raise RuntimeError("VAGRANT_IP_RANGE 가 설정되지 않았습니다.")

        used = await get_used_values(self.provider_name(), self.resource_type(), "ip")
        for ip in IP_POOL:
            if ip in used:
                continue
            if await self.infra_executor.is_ip_in_use(ip):
                self.logger.info(f"{ip} 는 DB 에 없지만 ping 응답이 있어 건너뜁니다.")
                continue
            return ip

        raise RuntimeError(f"VAGRANT_IP_RANGE 안에 사용할 수 있는 IP 가 없습니다. (전체 {len(IP_POOL)}개)")

    async def prepare_data(self, params: VagrantVMCreateInput) -> ProvisioningManifest:
        vars = params.model_dump(mode="json")
        vars.update(FLAVOR_SPECS[params.flavor])
        vars.update({
            "ip": await self._allocate_ip(),
            "box": os.getenv("VAGRANT_BOX", "ubuntu/jammy64"),
            "box_version": os.getenv("VAGRANT_BOX_VERSION") or None,
            "bridge": os.getenv("VAGRANT_BRIDGE") or None,
        })
        return ProvisioningManifest(vars=vars)

    async def prepare_update(self, params: Any, current: ProvisioningManifest) -> ProvisioningManifest:
        manifest = await super().prepare_update(params, current)
        # flavor 가 바뀌면 cpu/memory 도 함께 바꾼다. IP 등 할당된 값은 그대로 둔다.
        manifest.vars.update(FLAVOR_SPECS[Flavor(manifest.vars["flavor"])])
        return manifest

    def config_target(self, name: str, manifest: ProvisioningManifest) -> Optional[str]:
        return manifest.vars.get("ip")

    async def list_all(self) -> List[Dict[str, Any]]:
        """
        Vagrant 로 생성한 VM 리소스 목록 조회
        """
        resources = await super().list_all()

        for item in resources:
            try:
                metadata = json.loads(item.pop("metadata", None) or "{}")
            except (json.JSONDecodeError, TypeError):
                metadata = {}

            vars = metadata.get("vars", {})
            item["flavor"] = vars.get("flavor") or "N/A"
            item["purpose"] = vars.get("purpose") or "N/A"
            item["ip"] = vars.get("ip") or "N/A"
            item["box"] = vars.get("box") or "N/A"
            if metadata.get("last_error"):
                item["last_error"] = metadata["last_error"][-500:]

        return resources
