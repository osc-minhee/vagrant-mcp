# mcp_server/core/executor/ansible.py
import asyncio
import os
import json
import logging
from typing import Dict, Any, Optional, Tuple

class AnsibleExecutor:
    """
    Ansible 명령을 실행하는 클래스
    """
    def __init__(
        self,
        ansible_dir: str,
        default_playbook: str = None,
        default_inventory: str = None,
        remote_user: Optional[str] = None,
        private_key: Optional[str] = None,
    ):
        self.ansible_dir = ansible_dir
        self.default_playbook = default_playbook
        self.default_inventory = default_inventory
        self.remote_user = remote_user
        self.private_key = private_key
        self.logger = logging.getLogger(__name__)

    async def run_playbook(
        self,
        target_name: str,
        target_host: Optional[str] = None,
        playbook_name: Optional[str] = None,
        inventory_name: Optional[str] = None,
        extra_vars: Optional[Dict[str, Any]] = None,
        retries: int = 3
    ) -> Tuple[bool, str]:
        """
        특정 Target에 대해서만 Playbook을 실행하는 명령어 Wrapper.
        target_host(IP 등)가 있으면 그 주소 하나로 인벤토리를 만들고,
        없으면 인벤토리 파일에서 target_name 으로 대상을 찾는다.
        """
        p_name = playbook_name or self.default_playbook
        i_name = inventory_name or self.default_inventory

        if not p_name or not (target_host or i_name):
            raise ValueError(f"[{target_name}] Playbook 또는 Inventory 설정이 누락되었습니다.")

        self.logger.info(f"[{target_name}] Ansible Playbook '{p_name}'")

        # 1. 환경 변수 설정
        ansible_env = os.environ.copy()
        ansible_env["ANSIBLE_CONFIG"] = os.path.abspath(os.path.join(self.ansible_dir, "ansible.cfg"))

        # 2. 명령어 정의
        cmd = ["ansible-playbook", os.path.join("playbooks", p_name)]
        if target_host:
            # 마지막 쉼표가 있어야 파일 경로가 아닌 호스트 목록으로 인식한다.
            cmd.extend(["-i", f"{target_host},"])
        else:
            cmd.extend(["--limit", target_name, "-i", os.path.join("inventory", i_name)])

        if self.remote_user:
            cmd.extend(["-u", self.remote_user])
        if self.private_key:
            cmd.extend(["--private-key", self.private_key])
        if extra_vars:
            cmd.extend(["-e", json.dumps(extra_vars)])

        out_str = ""

        for i in range(retries):
            if i > 0:
                self.logger.info(f"[{target_name}] 접속 재시도 중... ({i}/{retries})")
                await asyncio.sleep(10)

            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    env=ansible_env,
                    cwd=self.ansible_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT
                )

                current_full_log = []

                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break

                    decoded_line = line.decode(errors='replace').strip()
                    if decoded_line:
                        self.logger.info(f"[{target_name}][ANSIBLE] {decoded_line}")
                        current_full_log.append(decoded_line)

                await process.wait()

                success = process.returncode == 0
                out_str = "\n".join(current_full_log)

                if success:
                    self.logger.info(f"[{target_name}] Ansible 프로비저닝 완료.")
                    return success, out_str
                else:
                    self.logger.error(f"[{target_name}] Ansible 프로비저닝 실패: \n{out_str}")

            except Exception as e:
                out_str = str(e)
                self.logger.error(f"Ansible 실행 중 오류 발생: {e}")

        return False, out_str
