#!/usr/bin/env bash
# ==============================================================================
# 李兆霖数学错题本 Web - Linux / macOS 启动脚本 (Bash)
# ==============================================================================
# 兼容: Ubuntu, Debian, CentOS, RHEL, Arch Linux, Alpine, macOS, WSL
# 用法:
#   ./scripts/start.sh              # 默认前台运行
#   ./scripts/start.sh --daemon     # 后台守护模式运行
#   ./scripts/start.sh --port 8080  # 自定义端口
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}==========================================================${NC}"
echo -e "${CYAN}  Math Notebook Web - Start Services (Linux / Unix)  ${NC}"
echo -e "${CYAN}==========================================================${NC}"

PORT=8000
HOST="127.0.0.1"
DAEMON=false
ENABLE_HARNESS_UI=true

while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--daemon)
            DAEMON=true
            shift
            ;;
        -p|--port)
            PORT="$2"
            shift 2
            ;;
        -h|--host)
            HOST="$2"
            shift 2
            ;;
        --no-ui)
            ENABLE_HARNESS_UI=false
            shift
            ;;
        *)
            echo -e "${RED}[✗] 未知参数: $1${NC}"
            echo "用法: $0 [--daemon] [--port 8000] [--host 127.0.0.1] [--no-ui]"
            exit 1
            ;;
    esac
done

if [[ -f "${PROJECT_ROOT}/.venv/bin/python" ]]; then
    PYTHON_EXE="${PROJECT_ROOT}/.venv/bin/python"
    echo -e "${GREEN}[OK] Using virtual environment: ${PYTHON_EXE}${NC}"
elif [[ -f "${PROJECT_ROOT}/.venv/bin/activate" ]]; then
    # shellcheck source=/dev/null
    source "${PROJECT_ROOT}/.venv/bin/activate"
    PYTHON_EXE="$(command -v python3 || command -v python)"
    echo -e "${GREEN}[OK] Activated virtual environment: ${PYTHON_EXE}${NC}"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_EXE="$(command -v python3)"
    echo -e "${YELLOW}[!] Virtual environment not found, using system Python3: ${PYTHON_EXE}${NC}"
elif command -v python >/dev/null 2>&1; then
    PYTHON_EXE="$(command -v python)"
    echo -e "${YELLOW}[!] Virtual environment not found, using system Python: ${PYTHON_EXE}${NC}"
else
    echo -e "${RED}[✗] Error: Python 3 is required. Please install Python 3.10+.${NC}"
    exit 1
fi

if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    echo -e "${GREEN}[OK] Configuration file loaded: .env${NC}"
else
    echo -e "${YELLOW}[!] .env file not found (you can copy from .env.example)${NC}"
fi

if command -v node >/dev/null 2>&1; then
    echo -e "${GREEN}[OK] Node.js detected: $(node -v)${NC}"
else
    echo -e "${YELLOW}[!] Node.js not detected, Harness UI may not load.${NC}"
fi

ARGS=("-X" "utf8" "-B" "scripts/local_env.py" "serve" "--host" "${HOST}" "--port" "${PORT}" "--enable-harness-model")
if [[ "${ENABLE_HARNESS_UI}" == true ]]; then
    ARGS+=("--enable-harness-ui")
fi

if [[ "${DAEMON}" == true ]]; then
    ARGS+=("--daemon")
    echo -e "${CYAN}[*] Starting services in background daemon mode...${NC}"
    "${PYTHON_EXE}" "${ARGS[@]}"
    sleep 2
    echo -e "${GREEN}[OK] Services started in background.${NC}"
    echo -e "     - Web API Gateway: ${CYAN}http://${HOST}:${PORT}${NC}"
    echo -e "     - Harness UI: ${CYAN}http://127.0.0.1:3080${NC}"
    echo -e "     - Log file: data/runtime/service.stdout.log"
else
    echo -e "${CYAN}[*] Starting services in foreground (Press Ctrl+C to stop)...${NC}"
    echo -e "     - Web URL: ${GREEN}http://${HOST}:${PORT}${NC}"
    echo -e "     - Harness UI: ${GREEN}http://127.0.0.1:3080${NC}"
    exec "${PYTHON_EXE}" "${ARGS[@]}"
fi
