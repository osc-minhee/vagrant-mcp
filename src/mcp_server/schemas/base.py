# mcp_server/schemas/base.py
from pydantic import BaseModel, Field, ConfigDict
from typing import Dict, Any

class ProvisioningManifest(BaseModel):
    # InfraExecutor 에 넘길 변수 (Vagrantfile 렌더링 값, Terraform 변수 등)
    vars: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True
    )
