#!/usr/bin/env bash
# Octopus 后端硬重启脚本（macOS / Linux / git-bash）
#
# 为什么需要硬重启：见 restart_backend.ps1 顶部
# 在 Windows git-bash 下，端口杀进程不能用 taskkill /F（git-bash 把 /F 当路径），
# 这个脚本对 Windows 平台会调用 powershell 来做杀进程。
#
# 用法：./restart_backend.sh

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
LOG_FILE="$PROJECT_ROOT/backend.log"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo ""
echo -e "${CYAN}🐙 Octopus 后端硬重启...${NC}"
echo ""

is_windows=false
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) is_windows=true ;;
esac

# Step 1: 杀掉所有 python 进程
echo -e "${YELLOW}[1/4] 杀掉 python 进程...${NC}"
if [ "$is_windows" = true ]; then
    # Windows 下走 powershell（taskkill /F 在 git-bash 会被当成路径）
    powershell -NoProfile -Command "Get-Process python* -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue" 2>/dev/null || true
else
    pkill -9 -f "uvicorn app.main:app" 2>/dev/null || true
fi

# Step 2: 等端口 8000 释放
echo -e "${YELLOW}[2/4] 等待端口 8000 释放...${NC}"
released=false
for i in $(seq 1 10); do
    sleep 1
    if [ "$is_windows" = true ]; then
        in_use=$(powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { 'yes' } else { 'no' }" 2>/dev/null | tr -d '\r')
        if [ "$in_use" = "no" ]; then
            released=true; break
        fi
        # 兜底：再补一刀
        if [ "$i" -ge 3 ]; then
            powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force -ErrorAction SilentlyContinue }" 2>/dev/null || true
        fi
    else
        if ! lsof -i :8000 -sTCP:LISTEN >/dev/null 2>&1; then
            released=true; break
        fi
    fi
done

if [ "$released" = false ]; then
    echo -e "${RED}    ✗ 端口 8000 仍被占用${NC}"
    exit 1
fi
echo -e "${GREEN}    ✓ 端口 8000 已释放${NC}"

# Step 3: 清 __pycache__
echo -e "${YELLOW}[3/4] 清理 __pycache__...${NC}"
find "$BACKEND_DIR/app" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
echo -e "${GREEN}    ✓ 已清理${NC}"

# Step 4: 启动 uvicorn
echo -e "${YELLOW}[4/4] 启动 uvicorn...${NC}"

# 项目里的 venv 是 Linux 风格的（venv/bin/activate），即便在 Windows git-bash 里也用同一份
# 不直接执行 venv/bin/python（在 Windows 下是 Linux ELF 跑不了），而是 activate 后调 python
ACTIVATE="$BACKEND_DIR/venv/bin/activate"
if [ ! -f "$ACTIVATE" ]; then
    ACTIVATE="$BACKEND_DIR/venv/Scripts/activate"
fi
if [ ! -f "$ACTIVATE" ]; then
    echo -e "${RED}    ✗ 找不到 venv activate 脚本${NC}"
    exit 1
fi

RELOAD_ARG="--reload"
if [ "${1:-}" = "--no-reload" ]; then
    RELOAD_ARG=""
fi

cd "$BACKEND_DIR"
# shellcheck disable=SC1090
source "$ACTIVATE"
nohup python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 $RELOAD_ARG > "$LOG_FILE" 2>&1 &
UVICORN_PID=$!
disown 2>/dev/null || true
echo -e "${GREEN}    ✓ uvicorn 启动 (PID $UVICORN_PID)，日志：$LOG_FILE${NC}"

# 健康检查
echo ""
echo -e "${CYAN}等待后端就绪...${NC}"
for i in $(seq 1 30); do
    sleep 1
    if curl -s http://localhost:8000/health 2>/dev/null | grep -q ok; then
        echo -e "${GREEN}✅ 后端已就绪 http://localhost:8000${NC}"
        exit 0
    fi
done

echo -e "${RED}⚠ 30 秒内未通过健康检查，请查看：$LOG_FILE${NC}"
exit 1
