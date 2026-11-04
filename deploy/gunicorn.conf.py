# gunicorn 生产配置 —— 海盈通保OA管理系统
# 启动命令：gunicorn -c deploy/gunicorn.conf.py app:app
# 说明：gunicorn 只监听本机回环地址，外网一律走 nginx 反代进来，
#       绝不直接暴露 gunicorn 端口，更安全。

import os

# 绑定本机回环（只有本机 nginx 能访问，外网碰不到）
bind = "127.0.0.1:5002"

# worker 数量：CPU 核心数*2+1。小白固定 4 个就够用
workers = 4
# 用最简单的 sync 模式，不引入 gevent/uvicorn 等额外依赖，最稳
worker_class = "sync"

# 单请求超时（秒）。OA 报表导出、上传附件可能慢，设 120 留余量
timeout = 120
graceful_timeout = 30
keepalive = 5

# 日志（目录需存在，start_prod.sh / systemd 会自动建 logs/）
accesslog = "logs/access.log"
errorlog = "logs/gunicorn.log"
loglevel = "info"

# 防内存泄漏：每处理 1000 个请求自动重启该 worker（±50 错峰）
max_requests = 1000
max_requests_jitter = 50

# 进程名前缀，方便 ps aux | grep 排查
proc_name = "haiyingtongbao"

def when_ready(server):
    server.log.info("海盈通保 gunicorn 已就绪")

def on_exit(server):
    server.log.info("海盈通保 gunicorn 已停止")
