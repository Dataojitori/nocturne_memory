#!/bin/sh
# =============================================================================
# Nocturne Memory - Heartbeat Agent 运行脚本
# 供 crontab 调用，包含锁机制、日志轮转、环境变量加载
# 兼容 Alpine (ash/BusyBox) 和标准 bash
# =============================================================================

set -eu

# ---------- 路径配置（根据实际部署修改） ----------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_SCRIPT="${SCRIPT_DIR}/heartbeat_agent.py"
LOG_DIR="${SCRIPT_DIR}/logs"
LOCK_FILE="/tmp/nocturne_heartbeat.lock"

# ---------- 日志轮转 ----------
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/heartbeat_$(date +%Y%m%d).log"

# 清理 7 天前的日志
find "${LOG_DIR}" -name "heartbeat_*.log" -mtime +7 -delete 2>/dev/null || true

# ---------- 锁机制（防止并发运行） ----------
if [ -f "${LOCK_FILE}" ]; then
    LOCK_PID=$(cat "${LOCK_FILE}" 2>/dev/null || echo "")
    if [ -n "${LOCK_PID}" ] && kill -0 "${LOCK_PID}" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 上一次运行仍在执行 (PID=${LOCK_PID})，跳过本次" >> "${LOG_FILE}"
        exit 0
    fi
    # 锁文件存在但进程已死，清理残留锁
    rm -f "${LOCK_FILE}"
fi

echo $$ > "${LOCK_FILE}"
trap 'rm -f "${LOCK_FILE}"' EXIT

# ---------- 环境变量 ----------
# 从 .env 文件加载（兼容 CRLF 换行符）
ENV_FILE="${SCRIPT_DIR}/../../.env"
if [ -f "${ENV_FILE}" ]; then
    while IFS='=' read -r key value; do
        # 去掉 \r，跳过注释和空行
        key=$(printf '%s' "$key" | tr -d '\r' | sed 's/^[[:space:]]*//')
        value=$(printf '%s' "$value" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        case "$key" in
            ''|\#*) continue ;;
        esac
        export "$key=$value"
    done < "${ENV_FILE}"
fi

# 方式2: 也可以直接在这里覆盖/硬编码（优先级更高）
# export HEARTBEAT_MCP_URL="http://127.0.0.1:8000/sse"
# export HEARTBEAT_API_KEY="your-api-key"
# export HEARTBEAT_API_BASE="https://open.bigmodel.cn/api/coding/paas/v4"
# export HEARTBEAT_MODEL="GLM-4.7"

# 同一台服务器，MCP 用 localhost 更稳定
export HEARTBEAT_MCP_URL="${HEARTBEAT_MCP_URL:-http://127.0.0.1:8000/sse}"

# ---------- 执行 ----------
echo "" >> "${LOG_FILE}"
echo "========== $(date '+%Y-%m-%d %H:%M:%S') 开始心跳整理 ==========" >> "${LOG_FILE}"

python3 "${AGENT_SCRIPT}" 2>&1 >> "${LOG_FILE}"

EXIT_CODE=$?
echo "========== $(date '+%Y-%m-%d %H:%M:%S') 运行结束 (exit=${EXIT_CODE}) ==========" >> "${LOG_FILE}"

exit ${EXIT_CODE}
