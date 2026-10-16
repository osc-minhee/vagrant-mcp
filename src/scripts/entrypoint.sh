#!/bin/bash
set -e

echo "컨테이너 초기화 및 환경 점검 시작..."

# 1. SSH 키 권한 고정 (agent_key: VM 접속용, host_key: Vagrant 호스트 접속용)
for key in /app/secrets/.ssh/agent_key /app/secrets/.ssh/host_key; do
    if [ -f "$key" ]; then
        echo "SSH Private Key 권한 설정: $(basename "$key")"
        chmod 600 "$key"
    fi
done

if [ ! -f /app/secrets/.ssh/agent_key.pub ]; then
    echo "경고: /app/secrets/.ssh/agent_key.pub 가 없습니다. VM 생성이 실패합니다." >&2
fi

mkdir -p /app/data

echo "환경변수 설정 중..."
export PYTHONPATH=$PYTHONPATH:/app

echo "환경 설정 완료. MCP Server 시작."
echo "------------------------------------------------"

exec python -m mcp_server.main
