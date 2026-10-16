# mcp_server/api/mcp_router.py
from fastapi import APIRouter
from mcp_server.api.mcp.vagrant import router as vagrant_router

router = APIRouter()

# MCP Tool APIs
router.include_router(vagrant_router, tags=["mcp"])
