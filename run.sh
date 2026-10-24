#!/usr/bin/env bash
# 车轮控制 Demo · 启动脚本
#   ./run.sh                  自动探测串口（找不到则仿真）
#   ./run.sh --sim            强制仿真演示
#   ./run.sh --port 8080      指定 Web 端口
#
# 没有可执行位（从 Windows 提交的仓库常见）时用：bash run.sh
# 等价的直接命令：python3 src/main.py
set -euo pipefail

cd "$(dirname "$0")"

# 关掉 Python 输出缓冲：输出被重定向/交给 systemd 时，启动横幅与访问地址要立刻可见
export PYTHONUNBUFFERED=1

# 选解释器：.venv 优先（install.sh 会建好并装依赖），其次 python3，最后 python
PY=""
for cand in .venv/bin/python .venv/bin/python3; do
  if [ -x "$cand" ]; then
    PY="$cand"
    break
  fi
done
if [ -z "$PY" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PY="$(command -v python)"
  else
    echo "[ERROR] 找不到 Python 3。Debian/Ubuntu 安装：sudo apt install python3" >&2
    echo "        首次部署请先执行：./install.sh（创建 .venv 并安装依赖）" >&2
    exit 1
  fi
fi

exec "$PY" src/main.py "$@"
