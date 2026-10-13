#!/bin/bash
# Octopus 开发环境停止脚本

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🛑 停止 Octopus 开发环境..."

# 从 PID 文件读取并终止进程
if [ -f .backend.pid ]; then
    BACKEND_PID=$(cat .backend.pid)
    if kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo "  停止后端 (PID: $BACKEND_PID)..."
        kill -9 "$BACKEND_PID" 2>/dev/null || true
    fi
    rm -f .backend.pid
fi

if [ -f .frontend.pid ]; then
    FRONTEND_PID=$(cat .frontend.pid)
    if kill -0 "$FRONTEND_PID" 2>/dev/null; then
        echo "  停止前端 (PID: $FRONTEND_PID)..."
        kill -9 "$FRONTEND_PID" 2>/dev/null || true
    fi
    rm -f .frontend.pid
fi

# 额外清理可能残留的进程
echo "  清理残留进程..."
pkill -f "uvicorn app.main:app" 2>/dev/null || true
pkill -f "vite" 2>/dev/null || true

echo "✅ Octopus 已停止"
