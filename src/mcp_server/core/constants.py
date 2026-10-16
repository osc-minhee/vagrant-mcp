# mcp_server/core/constants.py
import os
from enum import Enum
from pathlib import Path

# 소스 루트 (컨테이너: /app, 로컬: <repo>/src)
APP_DIR = Path(os.getenv("APP_DIR", Path(__file__).resolve().parents[2]))

# .env, SSH 키 등 Secret 파일 위치
SECRETS_DIR = Path(os.getenv("SECRETS_DIR", APP_DIR / "secrets"))

# VM 폴더가 들어갈 프로젝트 폴더 이름. 호스트의 경로로 쓰이므로 한 단계(슬래시 없음)로만 허용한다.
PROJECT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"

class ResourceState(str, Enum):
    """
    리소스의 LIFECYCLE 상태 정의
    """
    PENDING = "PENDING"
    PROVISIONING = "PROVISIONING"
    CONFIGURING = "CONFIGURING"
    RUNNING = "RUNNING"
    ERROR = "ERROR"
    DESTROYING = "DESTROYING"

    def __str__(self):
        return self.value

# 작업이 진행 중인 상태 (서버가 재시작되면 ERROR 로 복구한다)
TRANSIENT_STATES = (
    ResourceState.PENDING,
    ResourceState.PROVISIONING,
    ResourceState.CONFIGURING,
    ResourceState.DESTROYING,
)

# 변경/삭제 요청을 받을 수 있는 상태
STABLE_STATES = (ResourceState.RUNNING, ResourceState.ERROR)

class CloudProvider(str, Enum):
    """
    리소스를 할당할 CloudProvider 정의
    """
    VAGRANT = "vg"
    AWS = "aws"
    AZURE = "az"

    def __str__(self):
        return self.value

class Flavor(str, Enum):
    """
    리소스 사양 정의
    """
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"

    def __str__(self):
        return self.value
