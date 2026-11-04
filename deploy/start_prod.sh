#!/bin/bash
# 海盈通保OA管理系统 —— 生产一键启动（Linux，简易后台版）
#
# 适用：不想配 systemd 时的临时/测试启动。
# 正式生产环境推荐用 haiyingtongbao.service（开机自启 + 崩溃自愈）。
#
# 用法：bash deploy/start_prod.sh
# 停止：bash deploy/stop_prod.sh   （或 pkill -f haiyingtongbao）

set -e
# 切到项目根目录（deploy/ 的上一级）
cd "$(dirname "$0")/.."

# 激活虚拟环境（生产服务器先按 DEPLOY.md 建好 venv 并装依赖）
source venv/bin/activate

# 确保日志目录存在
mkdir -p logs

echo "============================================"
echo "  海盈通保OA管理系统 - 生产启动"
echo "  访问：https://<你的域名>"
echo "  日志：logs/gunicorn.log"
echo "============================================"
echo

# 后台守护方式启动 gunicorn
gunicorn -c deploy/gunicorn.conf.py app:app --daemon

echo "已启动。查看状态：ps aux | grep haiyingtongbao"
