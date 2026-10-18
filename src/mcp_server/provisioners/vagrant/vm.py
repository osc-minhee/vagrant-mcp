# mcp_server/provisioners/vagrant/vm.py
import asyncio
import ipaddress
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from mcp_server.core.base import ResourceProvisioner
from mcp_server.core.constants import APP_DIR, SECRETS_DIR, CloudProvider, Flavor, Image
from mcp_server.core.executor.ansible import AnsibleExecutor
from mcp_server.core.executor.vagrant import HostInspectError, VagrantExecutor
from mcp_server.core.hosts import VagrantHost, load_hosts
from mcp_server.core.redact import redact_text
from mcp_server.core.registry import ProvisionerRegistry
from mcp_server.database import get_resource, get_used_values
from mcp_server.schemas.base import ProvisioningManifest
from mcp_server.schemas.vagrant import VagrantVMCreateInput
from mcp_server.utils import get_env_int

FLAVOR_SPECS = {
    Flavor.SMALL: {"cpu": 1, "memory": 2048},
    Flavor.MEDIUM: {"cpu": 2, "memory": 4096},
    Flavor.LARGE: {"cpu": 4, "memory": 8192},
}

# 호스트에서 찾은 사용 중 IP 의 출처 -> 오류 메시지에 쓸 설명
_ON_HOST_REASONS = {
    "vagrantfile": "호스트의 다른 Vagrantfile 에 적혀 있습니다",
    "virtualbox": "호스트에서 실행 중인 다른 VirtualBox VM 이 쓰고 있습니다",
}

def _vars_of(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    DB 행의 metadata.vars. 읽지 못하면 빈 dict.
    """
    try:
        return json.loads(item.get("metadata") or "{}").get("vars", {})
    except (json.JSONDecodeError, TypeError, AttributeError):
        return {}

def _gi(mb: Optional[int]) -> Optional[float]:
    return None if mb is None else round(mb / 1024, 1)

def _mask_owners(vms: List[Dict[str, Any]], server_user: Optional[str]) -> None:
    """
    계정명이 응답에 나가지 않게 owner 를 별칭(이 서버의 SSH 계정은 server-account, 나머지는 user-1, user-2 ...)으로 바꾸고,
    project_dir 과 VM 이름 안의 계정명도 같은 별칭으로 바꾼다. 같은 계정은 같은 별칭이라 누구 VM 인지는 구분된다.
    vms 는 정렬된 상태로 받아야 별칭 번호가 매번 같게 붙는다.
    """
    aliases: Dict[str, str] = {}
    if server_user:
        aliases[server_user] = "server-account"
    for vm in vms:
        owner = vm.get("owner")
        if owner and owner not in aliases:
            aliases[owner] = f"user-{sum(1 for a in aliases.values() if a.startswith('user-')) + 1}"

    def swap(text: Optional[str]) -> Optional[str]:
        # 경로/이름을 구분자(/ _ . -) 기준으로 나눠 계정명과 정확히 같은 조각만 바꾼다.
        if not text:
            return text
        return redact_text("".join(aliases.get(part, part) for part in re.split(r"([/_.\-])", text)))

    for vm in vms:
        vm["owner"] = aliases.get(vm["owner"], vm["owner"])
        vm["project_dir"] = swap(vm["project_dir"])
        vm["name"] = swap(vm["name"])

def _pool_ranges(pool: List[str]) -> str:
    """
    IP 목록을 연속 구간으로 묶어 보여 준다. 예: 10.0.0.60-10.0.0.89, 10.0.0.100
    """
    runs: List[List[ipaddress.IPv4Address]] = []
    for ip in sorted(ipaddress.IPv4Address(i) for i in pool):
        if runs and int(ip) == int(runs[-1][1]) + 1:
            runs[-1][1] = ip
        else:
            runs.append([ip, ip])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)

# 이미지 -> Vagrant box. 사용자는 이미지 이름만 고르고 box/버전은 여기서 고정한다. (임의의 box 를 받지 않는다)
# 호스트에 box 가 없으면 첫 vagrant up 때 Vagrant Cloud 에서 받는다 (몇 분 걸림, 이후 캐시).
IMAGE_BOXES = {
    Image.UBUNTU_2204: {"box": "ubuntu/jammy64", "box_version": "20241002.0.0"},
    Image.ROCKY_8: {"box": "bento/rockylinux-8", "box_version": "202510.26.0"},
    Image.ROCKY_9: {"box": "bento/rockylinux-9", "box_version": "202510.26.0"},
}

if os.getenv("VAGRANT_BOX") or os.getenv("VAGRANT_BOX_VERSION"):
    logging.getLogger(__name__).warning(
        "VAGRANT_BOX/VAGRANT_BOX_VERSION 는 더 이상 쓰지 않습니다. 이미지는 vg_create_vm 의 image 로 고르고 box 는 IMAGE_BOXES 에 고정되어 있습니다."
    )

HOSTS = load_hosts()
SSH_DIR = SECRETS_DIR / ".ssh"

# host 를 비웠을 때 쓸 호스트 (VAGRANT_DEFAULT_HOST). 없으면 호스트가 1대일 때만 그 호스트를 쓴다.
_default_host_name = os.getenv("VAGRANT_DEFAULT_HOST", "").strip()
if _default_host_name and _default_host_name.lower() not in HOSTS:
    raise ValueError(
        f"VAGRANT_DEFAULT_HOST '{_default_host_name}' 가 호스트 목록에 없습니다. (사용 가능: {', '.join(h.name for h in HOSTS.values())})"
    )
DEFAULT_HOST: Optional[VagrantHost] = (
    HOSTS[_default_host_name.lower()] if _default_host_name
    else next(iter(HOSTS.values())) if len(HOSTS) == 1
    else None
)

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

    def _resolve_host(self, name: Optional[str], ip: Optional[str] = None) -> VagrantHost:
        """
        작업할 호스트. 순서: 요청한 host -> ip 가 속한 호스트(ip_ranges) -> 기본 호스트(VAGRANT_DEFAULT_HOST, 호스트가 1대면 그 호스트).
        host 와 ip 를 함께 주면 host 를 따르고, ip 가 그 호스트 대역 밖이면 IP 할당 단계에서 거절한다.
        """
        names = ", ".join(h.name for h in HOSTS.values())
        if name:
            host = HOSTS.get(name.strip().lower())
            if not host:
                raise ValueError(f"알 수 없는 호스트: {name} (사용 가능: {names})")
            return host
        if ip:
            matches = [h for h in HOSTS.values() if ip in h.ip_pool]
            if len(matches) == 1:
                return matches[0]
            if not matches:
                ranges = "; ".join(f"{h.name}: {_pool_ranges(h.ip_pool) or '대역 없음'}" for h in HOSTS.values())
                raise ValueError(f"IP {ip} 는 어느 호스트의 IP 대역에도 속하지 않습니다. ({ranges})")
            raise ValueError(
                f"IP {ip} 가 여러 호스트의 IP 대역({', '.join(h.name for h in matches)})에 속해 호스트를 정할 수 없습니다. host 를 지정하세요."
            )
        if DEFAULT_HOST:
            return DEFAULT_HOST
        raise ValueError(f"host 를 지정해야 합니다. 사용자에게 어느 호스트인지 물어보세요. (사용 가능: {names})")

    async def _host_used_ips(self, host: VagrantHost) -> Dict[str, str]:
        """
        그 호스트에서 사용 중인 IP (scan_root 아래 Vagrantfile + 실행 중인 VirtualBox VM). 조회하지 못하면 RuntimeError.
        """
        try:
            return await self.infra_executor.used_ips(host)
        except Exception as e:
            raise RuntimeError(redact_text(
                f"호스트 '{host.name}' 에서 사용 중인 IP 를 확인하지 못해 생성하지 않았습니다: {type(e).__name__}: {e}"
            )) from e

    def _resolve_target(self, name: Optional[str], ip_spec: Optional[str]) -> Tuple[VagrantHost, Optional[str]]:
        """
        호스트와 요청한 IP 를 정한다. ip_spec 은 전체 IP 또는 끝자리(75, *.*.*.75)다.
        끝자리면 host 를 줬을 때는 그 호스트 대역에서, 아니면 모든 호스트 대역에서 끝자리가 같은 IP 를 찾는다.
        """
        if not ip_spec:
            return self._resolve_host(name), None

        spec = ip_spec.strip()
        if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", spec):
            try:
                ip = str(ipaddress.IPv4Address(spec))
            except ValueError as e:
                raise ValueError(f"IP 형식이 올바르지 않습니다: {spec}") from e
            return self._resolve_host(name, ip), ip

        octet = int(spec.rsplit(".", 1)[-1])
        if octet > 255:
            raise ValueError(f"IP 끝자리는 0~255 여야 합니다: {spec}")
        hosts = [self._resolve_host(name)] if name else list(HOSTS.values())
        matches = [(h, ip) for h in hosts for ip in h.ip_pool if ip.rsplit(".", 1)[1] == str(octet)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            ranges = "; ".join(f"{h.name}: {_pool_ranges(h.ip_pool) or '대역 없음'}" for h in hosts)
            raise ValueError(f"끝자리가 {octet} 인 IP 가 IP 대역에 없습니다. ({ranges})")
        raise ValueError(
            f"끝자리가 {octet} 인 IP 가 여러 호스트에 있습니다: "
            + ", ".join(f"{h.name} {ip}" for h, ip in matches) + ". host 를 지정하세요."
        )

    async def _ip_usage(self, host: VagrantHost, candidates: List[str]) -> Dict[str, str]:
        """
        candidates 중 쓸 수 없는 IP 를 {IP: 이유} 로 돌려준다. 확인 기준 (생성, vg_check_ip 공통):
        1) 이 서버 DB 에 기록된 IP  2) 그 호스트 scan_root 아래 Vagrantfile 의 IP  3) 그 호스트에서 실행 중인 VirtualBox VM 의 IP
        네트워크의 다른 장비(ping 등)는 보지 않는다. 호스트를 조회하지 못하면 RuntimeError (모르는 채로 쓰지 않는다).
        """
        used = await get_used_values(self.provider_name(), self.resource_type(), "ip")
        on_host = await self._host_used_ips(host)

        busy: Dict[str, str] = {}
        for ip in candidates:
            if ip in used:
                busy[ip] = "이 서버가 만든 다른 VM 에 할당되어 있습니다"
            elif ip in on_host:
                busy[ip] = _ON_HOST_REASONS.get(on_host[ip], f"{on_host[ip]} 에서 사용 중입니다")
        return busy

    async def check_ip(self, host: Optional[str] = None, ip: Optional[str] = None) -> Dict[str, Any]:
        """
        IP 를 쓸 수 있는지 확인한다. (읽기 전용, 생성 때와 같은 기준)
        ip 를 주면 그 IP 하나의 available/reason, 없으면 그 호스트 대역에서 비어 있는 IP 를 구간으로 묶어 돌려준다.
        확인한 뒤 다른 요청이 먼저 가져갈 수 있으므로, 실제 생성 때 한 번 더 확인한다.
        """
        target, requested = self._resolve_target(host, ip)
        if not target.ip_pool:
            raise RuntimeError(f"호스트 '{target.name}' 에 IP 대역(ip_range)이 설정되지 않았습니다.")
        result: Dict[str, Any] = {"host": target.name, "ip_ranges": _pool_ranges(target.ip_pool)}

        if requested:
            result["ip"] = requested
            if requested not in target.ip_pool:
                result.update(available=False, reason=f"호스트 '{target.name}' 의 IP 대역 밖입니다")
                return result
            reason = (await self._ip_usage(target, [requested])).get(requested)
            result["available"] = reason is None
            if reason:
                result["reason"] = reason
            return result

        busy = await self._ip_usage(target, target.ip_pool)
        free = [candidate for candidate in target.ip_pool if candidate not in busy]
        result.update(
            total=len(target.ip_pool),
            available_count=len(free),
            available_ranges=_pool_ranges(free),
            unavailable_count=len(busy),
        )
        return result

    async def _allocate_ip(self, host: VagrantHost, requested: Optional[str] = None) -> str:
        """
        그 호스트의 ip_range 안에서 _ip_usage 기준으로 비어 있는 첫 IP 를 고른다.
        requested 가 있으면 그 IP 하나만 같은 기준으로 확인하고, 대역 밖이거나 사용 중이면 오류를 낸다. (다른 IP 로 바꾸지 않는다)
        """
        if not host.ip_pool:
            raise RuntimeError(f"호스트 '{host.name}' 에 IP 대역(ip_range)이 설정되지 않았습니다.")
        if requested and requested not in host.ip_pool:
            raise ValueError(
                f"IP {requested} 는 호스트 '{host.name}' 의 IP 대역 밖이라 쓸 수 없습니다. (대역: {_pool_ranges(host.ip_pool)})"
            )

        if requested:
            reason = (await self._ip_usage(host, [requested])).get(requested)
            if reason:
                raise RuntimeError(f"IP {requested} 는 사용할 수 없습니다: {reason}. 다른 IP 를 지정하거나 비워서 자동 할당하세요.")
            return requested

        busy = await self._ip_usage(host, host.ip_pool)
        for ip in host.ip_pool:
            if ip in busy:
                self.logger.info(f"[{host.name}] {ip} 는 건너뜁니다: {busy[ip]}")
                continue
            return ip

        raise RuntimeError(f"호스트 '{host.name}' 의 IP 대역 안에 사용할 수 있는 IP 가 없습니다. (전체 {len(host.ip_pool)}개)")

    async def _check_capacity(self, host: VagrantHost, flavor: Flavor, cpu: int) -> None:
        """
        호스트에 VM 을 올릴 여유가 있는지 확인하고, 없으면 RuntimeError 로 거절한다.
        기준은 일부러 느슨하다: 가용 메모리(MemAvailable) >= 요청 사양 + 예비, 디스크 여유 >= 최소 여유,
        vCPU <= 호스트 전체 CPU 수. (CPU 부하나 다른 VM 의 vCPU 합계는 보지 않는다)
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
        if cpu > info["cpu_total"]:
            raise RuntimeError(
                f"호스트 '{host.name}' 의 CPU 는 {info['cpu_total']}개라 vCPU {cpu}개를 줄 수 없습니다. {info['cpu_total']}개 이하로 지정하세요."
            )

    async def prepare_data(self, params: VagrantVMCreateInput) -> ProvisioningManifest:
        # 끝자리(75)로 준 ip 는 여기서 전체 IP 로 바뀐다.
        host, ip = self._resolve_target(params.host, params.ip)
        spec = FLAVOR_SPECS[params.flavor]
        # 요청의 cpu 가 있으면 flavor 의 vCPU 대신 쓴다. 메모리는 flavor 로만 정한다.
        cpu = params.cpu or spec["cpu"]
        await self._check_capacity(host, params.flavor, cpu)
        vars = params.model_dump(mode="json")
        vars.update(spec)
        vars["cpu"] = cpu
        vars.update({
            "host": host.name,
            # 요청의 project, 없으면 호스트의 기본 프로젝트. 둘 다 없으면 None (workdir 바로 아래)
            "project": params.project or host.default_project,
            # 요청의 ip(대역 안이고 비어 있어야 함), 없으면 대역에서 자동 할당
            "ip": await self._allocate_ip(host, ip),
            **IMAGE_BOXES[params.image],
            "bridge": host.bridge,
            # 있으면 Ansible 이 VM 기본 경로를 이 게이트웨이로 잡는다 (VPN 등 다른 대역에서 접속 가능)
            "gateway": host.gateway,
        })
        return ProvisioningManifest(vars=vars)

    async def prepare_update(self, params: Any, current: ProvisioningManifest) -> ProvisioningManifest:
        manifest = await super().prepare_update(params, current)
        spec = FLAVOR_SPECS[Flavor(manifest.vars["flavor"])]
        if params.flavor is not None:
            # flavor 를 바꾸면 memory 는 flavor 값, cpu 는 요청의 cpu 가 있으면 그것, 없으면 flavor 값.
            manifest.vars["memory"] = spec["memory"]
            manifest.vars["cpu"] = params.cpu or spec["cpu"]
        else:
            # cpu 만 바꾸면 위 super() 에서 이미 반영됐다. 값이 없는 기존 리소스는 flavor 값으로 채운다.
            manifest.vars.setdefault("cpu", spec["cpu"])
            manifest.vars.setdefault("memory", spec["memory"])
        # IP 등 할당된 값은 그대로 둔다.
        # host 기록이 없는 기존 리소스는 IP 로 찾은 호스트를 기록해 둔다. (찾지 못하면 변경하지 않고 오류)
        manifest.vars["host"] = self.infra_executor.host_of(manifest.vars).name
        return manifest

    def _host_name(self, vars: Dict[str, Any]) -> Optional[str]:
        """
        목록에 보여 줄 호스트 이름. 정할 수 없으면 None.
        """
        try:
            return self.infra_executor.host_of(vars).name
        except RuntimeError:
            return vars.get("host") or None

    def config_target(self, name: str, manifest: ProvisioningManifest) -> Optional[str]:
        return manifest.vars.get("ip")

    def config_vars(self, name: str, manifest: ProvisioningManifest) -> Dict[str, Any]:
        # 호스트 설정에 gateway 가 있으면 playbook 이 VM 의 기본 경로를 그쪽으로 잡는다. (VPN 등 다른 대역에서 접속)
        gateway = manifest.vars.get("gateway")
        return {"vm_gateway": gateway} if gateway else {}

    def _format(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        DB 행을 응답 형태로 바꾼다. metadata 는 풀어서 보여 줄 값만 남긴다.
        """
        try:
            metadata = json.loads(item.pop("metadata", None) or "{}")
        except (json.JSONDecodeError, TypeError):
            metadata = {}

        vars = metadata.get("vars", {})
        # host 기록이 없는 기존 리소스는 IP 가 속한 호스트. 정할 수 없으면 None.
        item["host"] = self._host_name(vars)
        item["project"] = vars.get("project") or None
        item["flavor"] = vars.get("flavor") or "N/A"
        # cpu 를 따로 지정했으면 flavor 와 다를 수 있어 실제 값을 함께 보여 준다.
        item["cpu"] = vars.get("cpu")
        item["memory_mb"] = vars.get("memory")
        item["purpose"] = vars.get("purpose") or "N/A"
        item["ip"] = vars.get("ip") or "N/A"
        # image 기록이 없는 기존 리소스는 box 만 보여 준다.
        item["image"] = vars.get("image") or "N/A"
        item["box"] = vars.get("box") or "N/A"
        if metadata.get("last_warning"):
            item["warning"] = redact_text(metadata["last_warning"])[-300:]
        if metadata.get("last_error"):
            # 가린 뒤에 자른다. 먼저 자르면 경로나 비밀값이 중간에 잘려 가려지지 않을 수 있다.
            item["last_error"] = redact_text(metadata["last_error"])[-500:]
        return item

    async def list_all(self, host: Optional[str] = None, state: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Vagrant 로 생성한 VM 리소스 목록 조회. host/state 를 주면 그것만 남긴다. (DB 기록 기준)
        """
        host_name = self._resolve_host(host).name.lower() if host else None
        resources = [self._format(item) for item in await super().list_all()]
        if host_name:
            resources = [r for r in resources if (r["host"] or "").lower() == host_name]
        if state:
            resources = [r for r in resources if r["state"] == str(state)]
        return resources

    async def get(self, name: str) -> Dict[str, Any]:
        """
        VM 한 대의 기록과, 그 호스트의 VirtualBox 에서 확인한 실제 상태(actual)를 함께 돌려준다.
        기록과 실제가 다르면 mismatch 에 이유를 담는다. 호스트에 접속하지 못하면 actual.checked=False.
        """
        resource = await get_resource(self.resource_id(name))
        if not resource:
            raise ValueError(f"VM '{name}' 을 찾을 수 없습니다. vg_list_vm 으로 이름을 확인하세요.")

        vars = _vars_of(resource)
        item = self._format(resource)
        item.pop("provider", None)

        try:
            host = self.infra_executor.host_of(vars)
            found = await self.infra_executor.list_vms(host, only=item["resource_id"])
        except Exception as e:
            item["actual"] = {"checked": False, "error": redact_text(f"{type(e).__name__}: {e}")}
            return item

        vm = found[0] if found else None
        actual: Dict[str, Any] = {"checked": True, "state": vm["state"] if vm else "missing"}
        if vm:
            actual.update(cpus=vm["cpus"], memory_mb=vm["memory_mb"], guest_ips=vm["guest_ips"])
        item["actual"] = actual

        # 작업 중(PENDING 등)이거나 ERROR 면 실제 상태가 무엇이든 이상하지 않다. RUNNING 기록만 비교한다.
        mismatch = []
        if item["state"] == "RUNNING":
            if actual["state"] != "running":
                mismatch.append(
                    f"기록은 RUNNING 이지만 VirtualBox 에서는 '{actual['state']}' 입니다. 호스트에서 직접 멈추거나 삭제했을 수 있습니다."
                )
            elif (vars.get("cpu"), vars.get("memory")) != (vm["cpus"], vm["memory_mb"]):
                mismatch.append(
                    f"사양이 기록({vars.get('cpu')} vCPU/{vars.get('memory')}MiB)과 다릅니다 "
                    f"(실제 {vm['cpus']} vCPU/{vm['memory_mb']}MiB)."
                )
        if mismatch:
            item["mismatch"] = " ".join(mismatch)
        return item

    async def list_host_vms(self, host: Optional[str] = None) -> Dict[str, Any]:
        """
        호스트의 VM 전체(이 서버가 만들지 않은 VM, 다른 계정의 VM 포함)와 상태.
        scan_root 아래 Vagrantfile, 실행 중인 VirtualBox 프로세스, SSH 계정의 VirtualBox 목록을 합친다.
        managed=True 면 이 서버가 만든 VM 이다.
        """
        target = self._resolve_host(host)

        try:
            inventory = await self.infra_executor.host_inventory(target)
        except Exception as e:
            raise RuntimeError(redact_text(
                f"호스트 '{target.name}' 의 VM 목록을 조회하지 못했습니다: {type(e).__name__}: {e}"
            )) from e

        # 이 서버가 만든 VM 은 VirtualBox 이름과 폴더 이름이 모두 resource_id(vg-<이름>) 이다.
        managed = {
            item["resource_id"]: item
            for item in await super().list_all()
            if (self._host_name(_vars_of(item)) or "").lower() == target.name.lower()
        }
        vms = inventory["vms"]
        for vm in vms:
            folder = (vm["project_dir"] or "").rsplit("/", 1)[-1]
            own = managed.get(vm["name"]) or managed.get(folder)
            vm["managed"] = own is not None
            if own:
                # vg_* 도구에서 쓰는 이름과 이 서버의 기록 상태
                vm["managed_name"] = own["name"]
                vm["managed_state"] = own["state"]
            vm["memory_gi"] = _gi(vm["memory_mb"])
        vms.sort(key=lambda v: (v["state"] != "running", str(v["owner"] or ""), str(v["project_dir"] or ""), str(v["name"] or "")))
        # managed 판별(VM/폴더 이름 비교)이 끝난 뒤에 계정명을 가린다.
        _mask_owners(vms, getattr(target.runner, "user", None))

        result = {
            "host": target.name,
            "total": len(vms),
            "running": sum(1 for v in vms if v["state"] == "running"),
            "managed": sum(1 for v in vms if v["managed"]),
            "vms": vms,
        }
        if inventory["notes"]:
            result["notes"] = [redact_text(n) for n in inventory["notes"]]
        return result

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
            info[dst] = _gi(info[src])
        return info

    async def list_hosts(self) -> List[Dict[str, Any]]:
        """
        Vagrant 호스트 목록과 각 호스트의 자원 현황, 이 서버가 만든 VM 수, 남은 IP 수를 조회한다.
        """
        resources = await super().list_all()

        states: Dict[str, Dict[str, int]] = {key: {} for key in HOSTS}
        used_ips = set()
        for item in resources:
            vars = _vars_of(item)
            if vars.get("ip"):
                used_ips.add(str(vars["ip"]))
            owner = str(self._host_name(vars) or "").lower()
            if owner in states:
                states[owner][item["state"]] = states[owner].get(item["state"], 0) + 1

        async def on_host_ips(host: VagrantHost):
            try:
                return await self.infra_executor.used_ips(host), None
            except Exception as e:
                return None, redact_text(f"{type(e).__name__}: {e}")

        statuses, scans = await asyncio.gather(
            asyncio.gather(*(self._host_status(h) for h in HOSTS.values())),
            asyncio.gather(*(on_host_ips(h) for h in HOSTS.values())),
        )

        hosts = []
        for host, status, (on_host, scan_error) in zip(HOSTS.values(), statuses, scans):
            by_state = states[host.name.lower()]
            item = {
                "host": host.name,
                # workdir(호스트 경로, 계정명 포함)는 LLM 이 알 필요가 없어서 응답에 넣지 않는다.
                "default_project": host.default_project,
                **status,
                # 이 서버(DB)가 만든 VM. 호스트에 있는 다른 VM 은 포함하지 않는다.
                "managed_vms": sum(by_state.values()),
                "managed_vms_by_state": by_state,
                "ip_pool_total": len(host.ip_pool),
            }
            # 남은 IP: 대역 중 DB 에도 없고, 그 호스트의 Vagrantfile/실행 중 VM 에서도 쓰지 않는 IP 수 (생성/vg_check_ip 와 같은 기준)
            others = {ip for ip in host.ip_pool if ip not in used_ips and on_host and ip in on_host}
            item["ip_available"] = sum(1 for ip in host.ip_pool if ip not in used_ips and ip not in others)
            # 대역 안인데 이 서버가 만들지 않은 VM 이 쓰고 있는 IP 수. 0 이 아니면 대역이 다른 VM 과 겹친다.
            item["ip_used_by_others"] = len(others)
            if scan_error:
                item["ip_scan_error"] = scan_error
            hosts.append(item)
        return hosts
