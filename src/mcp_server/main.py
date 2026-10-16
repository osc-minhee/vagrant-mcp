# mcp_server/main.py
import logging
import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi_mcp import FastApiMCP
from contextlib import asynccontextmanager

# Routers
from mcp_server.api.mcp_router import router as mcp_router

# Database
from mcp_server.database import init_db
from mcp_server.utils import get_env_int

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("MCP Server 가동 준비 중...")
    await init_db()
    yield
    logging.info("MCP Server를 종료합니다.")

app = FastAPI(
    title="Infrastructure Provisioning MCP",
    description="Vagrant 로 인프라 자원을 프로비저닝하는 도구가 정의된 MCP Server",
    version="2.0.0",
    lifespan=lifespan
)

@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    # 잘못된 요청(없는 리소스 등)은 원인을 그대로 돌려줘 Client(LLM)가 바로잡을 수 있게 한다.
    return JSONResponse(status_code=400, content={"success": False, "message": str(exc)})

@app.exception_handler(RuntimeError)
async def runtime_error_handler(request: Request, exc: RuntimeError):
    # 작업 중이라 거절, IP 소진 등 의도한 안내 메시지
    return JSONResponse(status_code=409, content={"success": False, "message": str(exc)})

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # 내부 정보가 Client 로 새지 않도록 상세 내용은 로그에만 남긴다.
    logger.exception(f"처리하지 못한 예외: {request.url.path}")
    return JSONResponse(status_code=500, content={"success": False, "message": "Internal Server Error"})

app.include_router(mcp_router)

# 라우트를 모두 등록한 뒤 생성해야 도구 목록에 포함된다. /mcp 에 Streamable HTTP 로 노출.
mcp = FastApiMCP(app, include_tags=["mcp"])
mcp.mount_http()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("SERVER_HOST", "0.0.0.0"), port=get_env_int("SERVER_PORT", 8000))
