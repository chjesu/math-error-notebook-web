#!/usr/bin/env bash
# ==============================================================================
# 李兆霖数学错题本 Web - Linux / macOS 停止脚本 (Bash)
# ==============================================================================
# 用法:
#   ./scripts/stop.sh           # 安全停止服务与本地数据库
#   ./scripts/stop.sh --force   # 强制停止并清理所有残留端口
# ==============================================================================

# 定位项目根目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}==========================================================${NC}"
echo -e "${CYAN}  李兆霖数学错题本 Web - 停止服务 (Linux / Unix)  ${NC}"
echo -e "${CYAN}==========================================================${NC}"

FORCE=false
if [[ "$1" == "-f" || "$1" == "--force" ]]; then
    FORCE=true
fi

# 1. 查找可用 Python
if [[ -f "${PROJECT_ROOT}/.venv/bin/python" ]]; then
    PYTHON_EXE="${PROJECT_ROOT}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_EXE="$(command -v python3)"
else
    PYTHON_EXE="$(command -v python || true)"
fi

# 2. 调用 local_env.py 安全停止数据库与会话
if [[ -n "${PYTHON_EXE}" && -x "${PYTHON_EXE}" ]]; then
    echo -e "${YELLOW}[*] 正在安全停止本地数据库和会话...${NC}"
    "${PYTHON_EXE}" -X utf8 -B scripts/local_env.py stop >/dev/null 2>&1 || true
fi

# 3. 停止通过 PID 文件记录的后台进程
PID_FILE="${PROJECT_ROOT}/data/runtime/service.pid"
if [[ -f "${PID_FILE}" ]]; then
    SAVED_PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${SAVED_PID}" ]] && kill -0 "${SAVED_PID}" >/dev/null 2>&1; then
        echo -e "${YELLOW}[*] 正在停止后台主进程 (PID: ${SAVED_PID})...${NC}"
        kill "${SAVED_PID}" >/dev/null 2>&1 || true
        sleep 1
        if kill -0 "${SAVED_PID}" >/dev/null 2>&1; then
            kill -9 "${SAVED_PID}" >/dev/null 2>&1 || true
        fi
    fi
    rm -f "${PID_FILE}"
fi

# 4. 检查并清理端口占用 (8000, 3080, 3307)
PORTS=(8000 3080 3307)
for PORT in "${PORTS[@]}"; do
    if command -v lsof >/dev/null 2>&1; then
        PIDS=$(lsof -ti tcp:"${PORT}" 2>/dev/null || true)
    elif command -v fuser >/dev/null 2>&1; then
        PIDS=$(fuser "${PORT}"/tcp 2>/dev/null || true)
    elif command -v ss >/dev/null 2>&1; then
        PIDS=$(ss -lptn "sport = :${PORT}" 2>/dev/null | grep -oP 'pid=\K\d+' || true)
    else
        PIDS=""
    fi

    if [[ -n "${PIDS}" ]]; then
        for PID in ${PIDS}; do
            echo -e "${YELLOW}[*] 正在释放端口 ${PORT} (PID: ${PID})...${NC}"
            kill -15 "${PID}" >/dev/null 2>&1 || true
            sleep 0.5
            kill -9 "${PID}" >/dev/null 2>&1 || true
        done
    fi
done

# 5. 强制模式下清理匹配的残留进程
if [[ "${FORCE}" == true ]]; then
    echo -e "${YELLOW}[*] 执行强制模式：扫描并清理残留进程...${NC}"
    pkill -f "scripts/local_env.py serve" >/dev/null 2>&1 || true
    pkill -f "deepseek-harness" >/dev/null 2>&1 || true
fi

echo -e "${GREEN}[✓] 所有服务已停止，端口资源已完全释放。${NC}"
