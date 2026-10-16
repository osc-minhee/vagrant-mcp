# mcp_server/database.py
import os
import json
import aiosqlite
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Iterable, Set
from mcp_server.core.constants import ResourceState, CloudProvider, TRANSIENT_STATES

logger = logging.getLogger(__name__)

# 환경 변수에서 DB 경로 로드 (기본값: 현재 디렉토리의 infra_state.db)
DB_PATH = os.getenv("DB_PATH", "./data/infra_state.db")

async def init_db():
    """
    데이터베이스 스키마 초기화.
    """
    logger.info("데이터베이스 초기화를 시작합니다.")

    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

    async with get_db_connection() as conn:
        await conn.executescript("""
            -- WAL 모드 활성화
            PRAGMA journal_mode=WAL;

            -- Resource 테이블 및 트리거 생성
            CREATE TABLE IF NOT EXISTS resource (
                resource_id TEXT PRIMARY KEY, -- 고유 식별자
                name TEXT NOT NULL UNIQUE, -- 리소스 이름
                provider TEXT NOT NULL, -- Cloud Provider(Vagrant, AWS, Azure 등)
                type TEXT NOT NULL, -- 리소스 유형(vm, lxc 등)
                state TEXT NOT NULL, -- 리소스 상태(PENDING, RUNNING 등)
                metadata TEXT, -- Provider별 식별 데이터 정보 (vars, last_error)
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP, -- 생성 시간
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP -- 갱신 시간
            );

            CREATE TRIGGER IF NOT EXISTS trg_resource_updated_at
            AFTER UPDATE ON resource
            BEGIN
                UPDATE resource SET updated_at = CURRENT_TIMESTAMP WHERE resource_id = OLD.resource_id;
            END;
        """)
        await conn.commit()

    await _recover_orphaned_states()
    logger.info("데이터베이스 스키마 및 초기 설정이 완료되었습니다.")

async def _recover_orphaned_states():
    """
    서버가 강제 종료되어 작업 중 상태로 남아있는 리소스를 ERROR 상태로 복구하는 함수.
    """
    states = [str(s) for s in TRANSIENT_STATES]
    placeholders = ", ".join("?" * len(states))

    async with get_db_connection() as conn:
        async with conn.execute(f"""
            UPDATE resource
            SET state = ?,
                metadata = json_set(COALESCE(metadata, '{{}}'), '$.last_error', '서버 재시작으로 작업이 중단되었습니다.')
            WHERE state IN ({placeholders})
        """, (str(ResourceState.ERROR), *states)) as cursor:
            if cursor.rowcount > 0:
                logger.warning(f"{cursor.rowcount}개의 고립된(Orphaned) 작업 상태를 'ERROR'로 초기화했습니다.")

        await conn.commit()

@asynccontextmanager
async def get_db_connection() -> AsyncGenerator[aiosqlite.Connection, None]:
    """
    DB 연결을 제공하는 Context Manager.
    """
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        db.row_factory = aiosqlite.Row
        yield db

async def get_used_values(provider: CloudProvider, resource_type: str, key: str) -> Set[str]:
    """
    metadata.vars.<key> 값 중 이미 사용 중인 값을 반환한다. (IP 등 중복 할당 방지용)
    """
    async with get_db_connection() as conn:
        async with conn.execute("""
            SELECT json_extract(metadata, '$.vars.' || ?) AS value
            FROM resource
            WHERE provider = ? AND type = ? AND value IS NOT NULL
        """, (key, str(provider), resource_type)) as cursor:
            rows = await cursor.fetchall()
            return {str(row["value"]) for row in rows}

async def register_new_resource(resource_id: str, name: str, provider: CloudProvider, resource_type: str, state: str, metadata: dict = None):
    """
    리소스 등록 함수.
    """
    if not isinstance(provider, CloudProvider):
        logger.warning(f"잘못된 Provider 타입이 입력됨: {type(provider)}")

    async with get_db_connection() as conn:
        await conn.execute("""
            INSERT INTO resource (resource_id, name, provider, type, state, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (resource_id, name, str(provider), resource_type, str(state), json.dumps(metadata or {})))

        await conn.commit()

async def update_resource_state(resource_id: str, state: str, metadata: dict = None):
    """
    리소스 상태 및 메타데이터 업데이트 함수.
    """
    async with get_db_connection() as conn:
        if metadata:
            await conn.execute("""
                UPDATE resource SET state = ?, metadata = ?
                WHERE resource_id = ?
            """, (str(state), json.dumps(metadata), resource_id))

        else:
            await conn.execute("""
                UPDATE resource SET state = ?
                WHERE resource_id = ?
            """, (str(state), resource_id))

        await conn.commit()

async def transition_state(resource_id: str, from_states: Iterable[str], to_state: str, metadata: dict = None) -> bool:
    """
    현재 상태가 from_states 중 하나일 때만 to_state 로 바꾼다. (동시 요청 방지)
    바뀌었으면 True 를 반환한다.
    """
    states = [str(s) for s in from_states]
    placeholders = ", ".join("?" * len(states))

    async with get_db_connection() as conn:
        if metadata:
            sql = f"UPDATE resource SET state = ?, metadata = ? WHERE resource_id = ? AND state IN ({placeholders})"
            args = (str(to_state), json.dumps(metadata), resource_id, *states)
        else:
            sql = f"UPDATE resource SET state = ? WHERE resource_id = ? AND state IN ({placeholders})"
            args = (str(to_state), resource_id, *states)

        async with conn.execute(sql, args) as cursor:
            changed = cursor.rowcount > 0
        await conn.commit()
        return changed

async def delete_resource(resource_id: str):
    """
    리소스 삭제 함수
    """
    async with get_db_connection() as conn:
        await conn.execute("DELETE FROM resource WHERE resource_id = ?", (resource_id,))
        await conn.commit()
        logger.info(f"[{resource_id}] 리소스 데이터 삭제 완료.")

async def get_resources_by_type(provider: CloudProvider, resource_type: str):
    """
    특정 Provider와 타입의 모든 리소스 조회.
    """
    async with get_db_connection() as conn:
        async with conn.execute("""
            SELECT resource_id, name, type, state, metadata, created_at, updated_at
            FROM resource
            WHERE provider = ? AND type = ?
            ORDER BY created_at
        """, (str(provider), resource_type)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def get_resource(resource_id: str) -> dict:
    """
    특정 리소스의 전체 정보를 DB에서 조회합니다.
    변경/삭제 시 메타데이터(vars)를 복원하기 위해 필수적인 함수입니다.
    """
    async with get_db_connection() as conn:
        async with conn.execute("""
            SELECT resource_id, name, provider, type, state, metadata, created_at, updated_at
            FROM resource
            WHERE resource_id = ?
        """, (resource_id,)) as cursor:
            row = await cursor.fetchone()

        if row:
            return dict(row)
        return None
