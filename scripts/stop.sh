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

# 2. 只由 local_env.py 裁决并验证完整服务树；失败时保留 PID 恢复证据。
if [[ -z "${PYTHON_EXE}" || ! -x "${PYTHON_EXE}" ]]; then
    echo -e "${RED}[✗] 未找到可用 Python，未执行停止。${NC}"
    exit 1
fi
echo -e "${YELLOW}[*] 正在安全停止完整服务树和本地数据库...${NC}"
if ! "${PYTHON_EXE}" -X utf8 -B scripts/local_env.py stop; then
    echo -e "${RED}[✗] 停止失败；恢复状态已保留。${NC}"
    exit 1
fi

echo -e "${GREEN}[✓] 所有服务已停止，端口资源已完全释放。${NC}"
