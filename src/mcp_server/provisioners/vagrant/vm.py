# mcp_server/provisioners/vagrant/vm.py
import asyncio
import json
import os
from typing import Any, Dict, List, Optional
from mcp_server.core.base import ResourceProvisioner
from mcp_server.core.constants import APP_DIR, SECRETS_DIR, CloudProvider, Flavor
from mcp_server.core.executor.ansible import AnsibleExecutor
from mcp_server.core.executor.vagrant import HostInspectError, VagrantExecutor
from mcp_server.core.hosts import VagrantHost, load_hosts
from mcp_server.core.redact import redact_text
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

HOSTS = load_hosts()
SSH_DIR = SECRETS_DIR / ".ssh"

vagrant_executor = VagrantExecutor(
    hosts=HOSTS,
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

    def _resolve_host(self, name: Optional[str]) -> VagrantHost:
        names = ", ".join(h.name for h in HOSTS.values())
        if name:
            host = HOSTS.get(name.strip().lower())
            if not host:
                raise ValueError(f"알 수 없는 호스트: {name} (사용 가능: {names})")
            return host
        if len(HOSTS) == 1:
            return next(iter(HOSTS.values()))
        raise ValueError(f"host 를 지정해야 합니다. 사용자에게 어느 호스트에 만들지 물어보세요. (사용 가능: {names})")

    async def _allocate_ip(self, host: VagrantHost) -> str:
        if not host.ip_pool:
            raise RuntimeError(f"호스트 '{host.name}' 에 IP 대역(ip_range)이 설정되지 않았습니다.")

        used = await get_used_values(self.provider_name(), self.resource_type(), "ip")
        for ip in host.ip_pool:
            if ip in used:
                continue
            if await self.infra_executor.is_ip_in_use(host, ip):
                self.logger.info(f"{ip} 는 DB 에 없지만 ping 응답이 있어 건너뜁니다.")
                continue
            return ip

        raise RuntimeError(f"호스트 '{host.name}' 의 IP 대역 안에 사용할 수 있는 IP 가 없습니다. (전체 {len(host.ip_pool)}개)")

    async def _check_capacity(self, host: VagrantHost, flavor: Flavor) -> None:
        """
        호스트에 VM 을 올릴 여유가 있는지 확인하고, 없으면 RuntimeError 로 거절한다.
        기준은 일부러 느슨하다: 가용 메모리(MemAvailable) >= 요청 사양 + 예비, 디스크 여유 >= 최소 여유. CPU 는 보지 않는다.
        오버커밋(실행 중 VM 할당 합계)은 따지지 않고, 이 서버가 방금 시작시킨 VM 은 아직 반영되지 않을 수 있다.
        VAGRANT_RESOURCE_CHECK=false 면 확인하지 않는다.
        """
        if os.getenv("VAGRANT_RESOURCE_CHECK", "true").lower() == "false":
            return

        try:
            info = await self.infra_executor.inspect_host(host)
        except HostInspectError as e:
            # 접속은 됐는데 수치를 읽지 못한 호스트(예: /proc 이 없는 OS)는 막지 않는다.
            self.logger.warning(f"[{host.name}] 자원 조회 결과를 쓸 수 없어 자원 확인을 건너뜁니다: {e}")
            return
        except Exception as e:
            raise RuntimeError(redact_text(
                f"호스트 '{host.name}' 에 접속하거나 자원을 조회하지 못해 생성하지 않았습니다: {type(e).__name__}: {e}"
            )) from e

        reserve = get_env_int("VAGRANT_HOST_MEM_RESERVE_MB", 512)
        min_disk = get_env_int("VAGRANT_MIN_DISK_FREE_MB", 5120)
        usable = info["mem_available_mb"] - reserve
        fits = [f.value for f, spec in FLAVOR_SPECS.items() if spec["memory"] <= usable]

        if FLAVOR_SPECS[flavor]["memory"] > usable:
            raise RuntimeError(
                f"호스트 '{host.name}' 의 가용 메모리가 부족합니다: 가용 {info['mem_available_mb']}MiB - 호스트 예비 {reserve}MiB = "
                f"{max(usable, 0)}MiB, 요청한 사양 {flavor.value} 는 {FLAVOR_SPECS[flavor]['memory']}MiB 가 필요합니다. "
                + (f"이 호스트에서 가능한 사양: {', '.join(fits)}." if fits else "이 호스트에는 어떤 사양도 올릴 수 없으니 다른 호스트를 고르세요.")
            )
        if info["disk_available_mb"] < min_disk:
            raise RuntimeError(
                f"호스트 '{host.name}' 의 디스크 여유가 부족합니다: {info['disk_available_mb']}MiB (최소 {min_disk}MiB 필요)."
            )

    async def prepare_data(self, params: VagrantVMCreateInput) -> ProvisioningManifest:
        host = self._resolve_host(params.host)
        await self._check_capacity(host, params.flavor)
        vars = params.model_dump(mode="json")
        vars.update(FLAVOR_SPECS[params.flavor])
        vars.update({
            "host": host.name,
            # 요청의 project, 없으면 호스트의 기본 프로젝트. 둘 다 없으면 None (workdir 바로 아래)
            "project": params.project or host.default_project,
            "ip": await self._allocate_ip(host),
            "box": os.getenv("VAGRANT_BOX", "ubuntu/jammy64"),
            "box_version": os.getenv("VAGRANT_BOX_VERSION") or None,
            "bridge": host.bridge,
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
            # host 기록이 없는 기존 리소스는 기본 호스트에 있다.
            item["host"] = vars.get("host") or self.infra_executor.default_host.name
            item["project"] = vars.get("project") or None
            item["flavor"] = vars.get("flavor") or "N/A"
            item["purpose"] = vars.get("purpose") or "N/A"
            item["ip"] = vars.get("ip") or "N/A"
            item["box"] = vars.get("box") or "N/A"
            if metadata.get("last_error"):
                # 가린 뒤에 자른다. 먼저 자르면 경로나 비밀값이 중간에 잘려 가려지지 않을 수 있다.
                item["last_error"] = redact_text(metadata["last_error"])[-500:]

        return resources

    async def _host_status(self, host: VagrantHost) -> Dict[str, Any]:
        """
        호스트 한 대의 자원 현황. 접속 실패는 reachable=False 와 error 로 돌려준다.
        """
        try:
            info = await self.infra_executor.inspect_host(host)
        except Exception as e:
            return {"reachable": False, "error": redact_text(f"{type(e).__name__}: {e}")}

        info["reachable"] = True
        # 사람이 읽기 쉬운 단위(GiB). 계산은 *_mb 로 한다.
        for src, dst in (
            ("mem_total_mb", "mem_total_gi"),
            ("mem_available_mb", "mem_available_gi"),
            ("vbox_mem_allocated_mb", "vbox_mem_allocated_gi"),
            ("disk_total_mb", "disk_total_gi"),
            ("disk_available_mb", "disk_available_gi"),
        ):
            info[dst] = None if info[src] is None else round(info[src] / 1024, 1)
        return info

    async def list_hosts(self) -> List[Dict[str, Any]]:
        """
        Vagrant 호스트 목록과 각 호스트의 자원 현황, 이 서버가 만든 VM 수, 남은 IP 수를 조회한다.
        """
        resources = await super().list_all()

        default_name = self.infra_executor.default_host.name
        states: Dict[str, Dict[str, int]] = {key: {} for key in HOSTS}
        used_ips = set()
        for item in resources:
            try:
                vars = json.loads(item.get("metadata") or "{}").get("vars", {})
            except (json.JSONDecodeError, TypeError):
                vars = {}
            if vars.get("ip"):
                used_ips.add(str(vars["ip"]))
            owner = str(vars.get("host") or default_name).lower()
            if owner in states:
                states[owner][item["state"]] = states[owner].get(item["state"], 0) + 1

        statuses = await asyncio.gather(*(self._host_status(h) for h in HOSTS.values()))

        hosts = []
        for host, status in zip(HOSTS.values(), statuses):
            by_state = states[host.name.lower()]
            hosts.append({
                "host": host.name,
                # workdir(호스트 경로, 계정명 포함)는 LLM 이 알 필요가 없어서 응답에 넣지 않는다.
                "default_project": host.default_project,
                **status,
                # 이 서버(DB)가 만든 VM. 호스트에 있는 다른 VM 은 포함하지 않는다.
                "managed_vms": sum(by_state.values()),
                "managed_vms_by_state": by_state,
                # 대역 중 DB 에 할당되지 않은 IP 수. ping 으로 확인하기 전의 값이라 실제 생성 때 더 줄 수 있다.
                "ip_pool_total": len(host.ip_pool),
                "ip_available": sum(1 for ip in host.ip_pool if ip not in used_ips),
            })
        return hosts
