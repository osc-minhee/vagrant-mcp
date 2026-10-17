# mcp_server/core/base.py
import json
import logging
import asyncio
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Set
from sqlite3 import IntegrityError
from mcp_server import database as db_service
from mcp_server.core.constants import ResourceState, CloudProvider, STABLE_STATES
from mcp_server.core.executor.ansible import AnsibleExecutor
from mcp_server.core.executor.base import InfraExecutor
from mcp_server.core.notify import notify
from mcp_server.core.redact import redact_text
from mcp_server.schemas.base import ProvisioningManifest

# 자원(IP 등) 할당 ~ DB 기록 구간을 직렬화한다.-> 한번에 한 요청만 지나가게 함 
_alloc_lock = asyncio.Lock()

# create_task 의 결과를 참조하지 않으면 실행 중에 GC 될 수 있으므로 보관한다.
_background_tasks: Set[asyncio.Task] = set()

def _spawn(coro) -> None:
    """ 백그라운드 실행 """
    task = asyncio.create_task(coro) # 작업을 던져놓고 바로 다음줄로 이동 
    _background_tasks.add(task) # 참조를 set에 보관 
    task.add_done_callback(_background_tasks.discard)

def _tail(text: str, limit: int = 1000) -> str:
    """" 에러 원인 남기는 함수 (뒤쪽 1000자)"""
    # 가린 뒤에 자른다. 먼저 자르면 비밀값/주소가 중간에 잘려 가려지지 않을 수 있다. (원문은 실행 로그에 남는다)
    text = redact_text((text or "").strip())
    return text if len(text) <= limit else "..." + text[-limit:]

def _label(resource_id: str, manifest: ProvisioningManifest) -> str:
    # 알림에 쓸 이름. 요청 때의 VM 이름, 없으면 resource_id.
    return str(manifest.vars.get("name") or resource_id)

def _where(manifest: ProvisioningManifest) -> str:
    # 알림에 붙일 위치 (호스트, IP). 없으면 빈 문자열.
    parts = [str(manifest.vars[k]) for k in ("host", "ip") if manifest.vars.get(k)]
    return f" ({', '.join(parts)})" if parts else ""

class ResourceProvisioner(ABC):
    """
    처리 순서 흐름을 정해둔 틀 
    모든 인프라 리소스 프로비지너의 최상위 추상 클래스.
    VM, LXC, Network 등 모든 리소스 관리 클래스는 이 클래스를 상속받아야 한다.
    """
    def __init__(self):
        self.logger = logging.getLogger(f"{self.__class__.__module__}.{self.__class__.__name__}")

    @property
    @abstractmethod
    def infra_executor(self) -> InfraExecutor:
        """
        리소스를 생성/변경/삭제하는 Executor (Vagrant, Terraform 등) - 실제 명령을 수행 
        """
        pass

    @property
    def config_executor(self) -> Optional[AnsibleExecutor]:
        """
        생성 후 환경을 구성하는 Executor. None 이면 CONFIGURING 단계를 건너뛴다.
        """
        return None

    @classmethod
    @abstractmethod
    def provider_name(cls) -> CloudProvider:
        """
        Provider(Vagrant, AWS, Azure 등)를 반환한다.
        """
        pass

    @classmethod
    @abstractmethod
    def resource_type(cls) -> str:
        """
        리소스 타입 (예: 'vm', 'lxc')
        """
        pass

    @abstractmethod
    async def prepare_data(self, params: Any) -> ProvisioningManifest:
        """
        생성 시 Executor 변수 및 메타데이터를 준비하는 Hook. (IP 등 자원 할당 포함)
        params 매개변수는 Pydantic 객체 형태로 들어온다.
        """
        pass

    async def prepare_update(self, params: Any, current: ProvisioningManifest) -> ProvisioningManifest:
        """
        변경 시 기존 값에 요청 값(None 이 아닌 필드)만 덮어쓴다. 할당된 자원은 그대로 유지한다.
        """
        changes = params.model_dump(mode="json", exclude_none=True, exclude={"name"})
        return current.model_copy(update={"vars": {**current.vars, **changes}})

    def config_target(self, name: str, manifest: ProvisioningManifest) -> Optional[str]:
        """
        환경 구성(Ansible) 대상 주소. None 이면 인벤토리에서 name 으로 찾는다.
        """
        return None

    def resource_id(self, name: str) -> str:
        return f"{self.provider_name()}-{name}"

    async def create(self, params: Any) -> Dict[str, Any]:
        """
        리소스를 생성하는 함수
        """
        resource_id = self.resource_id(params.name)

        # LIFECYCLE - PENDING
        try:
            await self.register_resource(resource_id, params.name, ResourceState.PENDING)
        except IntegrityError:
            self.logger.info(f"[{resource_id}] 중복 요청 감지 (Unique 제약 조건 위반).")
            existing = await db_service.get_resource(resource_id)
            state = existing.get("state") if existing else "UNKNOWN"
            return {
                "success": False,
                "resource_id": resource_id,
                "message": f"'{params.name}' 리소스가 이미 '{state}' 상태로 존재합니다."
            }

        try:
            async with _alloc_lock:
                manifest = await self.prepare_data(params)
                # LIFECYCLE - PROVISIONING
                await self.update_state(resource_id, ResourceState.PROVISIONING, manifest.model_dump())
        except Exception as e:
            # 아직 아무것도 만들지 않았으므로 기록을 지워 같은 이름으로 다시 요청할 수 있게 한다.
            await self.unregister_resource(resource_id)
            raise RuntimeError(f"'{params.name}' 생성 준비 실패: {e}") from e

        _spawn(self._run_create(resource_id, params.name, manifest))

        return {
            "success": True,
            "resource_id": resource_id,
            "vars": manifest.vars,
            "message": f"'{params.name}' 생성이 시작되었습니다. 목록 조회로 진행 상태를 확인할 수 있습니다.",
        }

    async def _run_create(self, resource_id: str, name: str, manifest: ProvisioningManifest):
        try:
            ok, out, err = await self.infra_executor.apply(resource_id, manifest.vars)
            if not ok:
                raise RuntimeError(f"생성 실패: {_tail(err or out)}")

            # 명령이 성공했다고 해도 실제로 떠 있는지 확인한 뒤에 다음 단계로 간다.
            ok, message = await self.infra_executor.verify_applied(resource_id, manifest.vars)
            if not ok:
                raise RuntimeError(f"생성 확인 실패: {message}")
            if message:
                self.logger.warning(f"[{resource_id}] {message}")
                manifest = manifest.model_copy(update={"last_warning": message})
                await self.update_state(resource_id, ResourceState.PROVISIONING, manifest.model_dump())

            self.logger.info(f"[{resource_id}] {self.resource_type().upper()} 리소스 생성 완료.")

            if self.config_executor:
                # LIFECYCLE - CONFIGURING
                await self.update_state(resource_id, ResourceState.CONFIGURING)

                ok, out = await self.config_executor.run_playbook(
                    target_name=name, target_host=self.config_target(name, manifest)
                )
                if not ok:
                    raise RuntimeError(f"환경 구성 실패: {_tail(out)}")

            # LIFECYCLE - RUNNING
            await self.update_state(resource_id, ResourceState.RUNNING)
            self.logger.info(f"[{resource_id}] 모든 프로비저닝 작업 완료.")
            warning = getattr(manifest, "last_warning", None)
            await notify(f"✅ '{name}' 생성 완료{_where(manifest)}" + (f"\n⚠️ {warning}" if warning else ""))

        # LIFECYCLE - ERROR
        except Exception as e:
            self.logger.error(f"[{resource_id}] {self.resource_type().upper()} 리소스 생성 중 오류가 발생하였습니다: {e}")
            await self.update_state(resource_id, ResourceState.ERROR, {**manifest.model_dump(), "last_error": str(e)})
            await notify(f"❌ '{name}' 생성 실패{_where(manifest)}: {e}", error=True)

    async def update(self, params: Any) -> Dict[str, Any]:
        """
        기존 리소스의 설정(스펙 등)을 반영하는 함수
        """
        resource_id = self.resource_id(params.name)
        resource = await db_service.get_resource(resource_id)
        if not resource:
            raise ValueError(f"업데이트할 리소스({params.name})를 찾을 수 없습니다.")

        current = ProvisioningManifest.model_validate(json.loads(resource["metadata"] or "{}"))
        new = await self.prepare_update(params, current)
        new_metadata = new.model_dump(exclude={"last_error"})

        if not await db_service.transition_state(resource_id, STABLE_STATES, ResourceState.PROVISIONING, new_metadata):
            raise RuntimeError(f"'{params.name}' 는 현재 작업 중이라 변경할 수 없습니다. 작업이 끝난 뒤 다시 시도하세요.")

        _spawn(self._run_update(resource_id, current, new))

        return {
            "success": True,
            "resource_id": resource_id,
            "vars": new.vars,
            "message": f"'{params.name}' 업데이트 프로세스를 시작했습니다."
        }

    async def _run_update(self, resource_id: str, current: ProvisioningManifest, new: ProvisioningManifest):
        try:
            self.logger.info(f"[{resource_id}] 변경된 설정으로 업데이트를 시작합니다.")
            ok, out, err = await self.infra_executor.apply(resource_id, new.vars)
            if not ok:
                raise RuntimeError(f"업데이트 실패: {_tail(err or out)}")

            ok, message = await self.infra_executor.verify_applied(resource_id, new.vars)
            if not ok:
                raise RuntimeError(f"업데이트 확인 실패: {message}")
            if message:
                self.logger.warning(f"[{resource_id}] {message}")
            # 이전 경고는 지우고, 이번 확인의 경고만 남긴다.
            await self.update_state(resource_id, ResourceState.RUNNING, {**new.model_dump(exclude={"last_warning"}), **({"last_warning": message} if message else {})})
            await notify(f"✅ '{_label(resource_id, new)}' 변경 완료{_where(new)}" + (f"\n⚠️ {message}" if message else ""))

        except Exception as e:
            self.logger.error(f"[{resource_id}] 업데이트 중 오류 발생: {e}")
            # 반영되지 않았을 수 있으므로 이전 설정을 남겨 둔다.
            await self.update_state(resource_id, ResourceState.ERROR, {**current.model_dump(), "last_error": str(e)})
            await notify(f"❌ '{_label(resource_id, current)}' 변경 실패{_where(current)}: {e}", error=True)

    async def destroy(self, params: Any) -> Dict[str, Any]:
        """
        리소스 삭제
        """
        resource_id = self.resource_id(params.name)
        resource = await db_service.get_resource(resource_id)
        if not resource:
            raise ValueError(f"삭제할 리소스({params.name})를 찾을 수 없습니다.")

        if not await db_service.transition_state(resource_id, STABLE_STATES, ResourceState.DESTROYING):
            raise RuntimeError(f"'{params.name}' 는 현재 작업 중이라 삭제할 수 없습니다. 작업이 끝난 뒤 다시 시도하세요.")

        manifest = ProvisioningManifest.model_validate(json.loads(resource["metadata"] or "{}"))
        _spawn(self._run_destroy(resource_id, manifest))

        return {
            "success": True,
            "resource_id": resource_id,
            "message": f"'{params.name}' 삭제 프로세스를 시작했습니다."
        }

    async def _run_destroy(self, resource_id: str, manifest: ProvisioningManifest):
        try:
            self.logger.info(f"[{resource_id}] 리소스 삭제 프로세스 시작...")
            ok, out, err = await self.infra_executor.destroy(resource_id, manifest.vars)
            if not ok:
                raise RuntimeError(f"삭제 실패: {_tail(err or out)}")

            # 실제로 사라진 것을 확인한 뒤에만 기록을 지운다. (남아 있으면 ERROR 로 두어 다시 삭제할 수 있게 한다)
            ok, message = await self.infra_executor.verify_destroyed(resource_id, manifest.vars)
            if not ok:
                raise RuntimeError(f"삭제 확인 실패: {message}")
            if message:
                self.logger.warning(f"[{resource_id}] {message}")

            await self.unregister_resource(resource_id)
            self.logger.info(f"[{resource_id}] 리소스 삭제 완료.")
            await notify(f"🗑️ '{_label(resource_id, manifest)}' 삭제 완료{_where(manifest)}" + (f"\n⚠️ {message}" if message else ""))

        except Exception as e:
            self.logger.error(f"[{resource_id}] 삭제 실패: {e}")
            await self.update_state(resource_id, ResourceState.ERROR, {**manifest.model_dump(), "last_error": str(e)})
            await notify(f"❌ '{_label(resource_id, manifest)}' 삭제 실패{_where(manifest)}: {e}", error=True)

    async def list_all(self) -> List[Dict[str, Any]]:
        """
        리소스 목록 조회
        """
        return await db_service.get_resources_by_type(
            self.provider_name(), self.resource_type()
        )

    async def register_resource(self, resource_id: str, name: str, state: ResourceState, metadata: Optional[dict] = None):
        """
        Provisioner가 호출할 리소스 상태 등록 Helper
        """
        await db_service.register_new_resource(resource_id, name, self.provider_name(), self.resource_type(), state, metadata)

    async def update_state(self, resource_id: str, state: ResourceState, metadata: Optional[dict] = None):
        """
        Provisioner가 호출할 리소스 상태 업데이트 Helper
        """
        await db_service.update_resource_state(resource_id, state, metadata)

    async def unregister_resource(self, resource_id: str):
        """
        Provisioner가 호출할 리소스 삭제 Helper
        """
        await db_service.delete_resource(resource_id)
