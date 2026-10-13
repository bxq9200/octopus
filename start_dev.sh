#!/bin/bash
# Octopus 开发环境一键启动脚本
# 用法: ./start_dev.sh

set -e

echo "🐙 Octopus 开发环境启动..."

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 颜色定义
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# 检查端口占用
function check_port() {
    local port=$1
    if lsof -Pi :"$port" -sTCP:LISTEN -t >/dev/null 2>&1; then
        echo -e "${YELLOW}⚠️ 端口 $port 已被占用${NC}"
        return 1
    fi
    return 0
}

# 杀死占用端口的进程
function kill_port() {
    local port=$1
    local pid
    pid=$(lsof -Pi :"$port" -sTCP:LISTEN -t 2>/dev/null | head -1)
    if [ -n "$pid" ]; then
        echo -e "${YELLOW}   终止进程 PID: $pid${NC}"
        kill -9 "$pid" 2>/dev/null || true
        sleep 1
    fi
}

# 检查并清理端口
echo "🔍 检查端口..."
BACKEND_PORT=8000
FRONTEND_PORT=5173

if ! check_port $BACKEND_PORT; then
    kill_port $BACKEND_PORT
fi

if ! check_port $FRONTEND_PORT; then
    kill_port $FRONTEND_PORT
fi

# 启动后端
echo -e "${GREEN}🚀 启动后端 (port $BACKEND_PORT)...${NC}"
cd "$SCRIPT_DIR/backend"
source venv/bin/activate

# 检查 .env 文件
if [ ! -f .env ]; then
    echo -e "${YELLOW}⚠️ 缺少 .env 文件，使用 .env.example 复制${NC}"
    cp .env.example .env 2>/dev/null || echo "OPENAI_API_KEY=sk-test" > .env
fi

# 后台启动后端
python -m uvicorn app.main:app --host 0.0.0.0 --port $BACKEND_PORT --reload > "$SCRIPT_DIR/backend.log" 2>&1 &
BACKEND_PID=$!
echo $BACKEND_PID > "$SCRIPT_DIR/.backend.pid"

# 等待后端启动
echo "⏳ 等待后端就绪..."
for i in {1..30}; do
    if curl -s http://localhost:$BACKEND_PORT/health >/dev/null 2>&1; then
        echo -e "${GREEN}✅ 后端已就绪${NC}"
        break
    fi
    sleep 1
    if [ $i -eq 30 ]; then
        echo -e "${RED}❌ 后端启动超时，请检查 backend.log${NC}"
        exit 1
    fi
done

# 启动前端
echo -e "${GREEN}🚀 启动前端 (port $FRONTEND_PORT)...${NC}"
cd "$SCRIPT_DIR/frontend"

# 检查 node_modules
if [ ! -d node_modules ]; then
    echo -e "${YELLOW}⚠️ 缺少 node_modules，正在安装...${NC}"
    npm install
fi

# 后台启动前端
npx vite --host 0.0.0.0 --port $FRONTEND_PORT > "$SCRIPT_DIR/frontend.log" 2>&1 &
FRONTEND_PID=$!
echo $FRONTEND_PID > "$SCRIPT_DIR/.frontend.pid"

# 等待前端启动
echo "⏳ 等待前端就绪..."
for i in {1..30}; do
    if curl -s http://localhost:$FRONTEND_PORT >/dev/null 2>&1; then
        echo -e "${GREEN}✅ 前端已就绪${NC}"
        break
    fi
    sleep 1
    if [ $i -eq 30 ]; then
        echo -e "${RED}❌ 前端启动超时，请检查 frontend.log${NC}"
        exit 1
    fi
done

echo ""
echo -e "${GREEN}═══════════════════════════════════════${NC}"
echo -e "${GREEN}  🐙 Octopus 开发环境已启动！${NC}"
echo -e "${GREEN}═══════════════════════════════════════${NC}"
echo ""
echo -e "  前端: ${YELLOW}http://localhost:$FRONTEND_PORT${NC}"
echo -e "  后端: ${YELLOW}http://localhost:$BACKEND_PORT${NC}"
echo -e "  API文档: ${YELLOW}http://localhost:$BACKEND_PORT/docs${NC}"
echo ""
echo -e "  日志文件:"
echo -e "    - 后端: ${YELLOW}backend.log${NC}"
echo -e "    - 前端: ${YELLOW}frontend.log${NC}"
echo ""
echo -e "  停止服务: ${YELLOW}./stop_dev.sh${NC}"
echo ""

# 保持脚本运行（可选，如果需要前台运行）
# wait $BACKEND_PID $FRONTEND_PID
