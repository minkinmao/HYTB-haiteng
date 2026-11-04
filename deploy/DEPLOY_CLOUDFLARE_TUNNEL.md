# 海盈通保OA · Cloudflare Tunnel 部署手册（oa.crmwinpro.com）

> 适用场景：域名 `crmwinpro.com` 已接入 Cloudflare，且 `oa.crmwinpro.com` 的 DNS
> 记录已指向 Cloudflare Tunnel（`haiying-oa`，已代理）。本手册走 **隧道模式**，
> 因此**不需要公网 IP、不开 80/443、不申请证书、不配 nginx 反代**。
> 服务器只要能出网（cloudflared 主动连 Cloudflare）即可。

---

## 一、Cloudflare 仪表盘（需登录账号，约 3 分钟）

1. 右上角头像 → 进入 **Zero Trust**（若首次进入按提示完成开通，免费版即可）。
2. 左侧 ** Networks（网络） → Tunnels（隧道）** → 找到 **`haiying-oa`**。
3. 点 `haiying-oa` → **Configure（配置） → Public Hostname（公共主机名）**：
   - 若列表里**没有** `oa.crmwinpro.com`，点「Add a public hostname」：
     - Subdomain：`oa`
     - Domain：`crmwinpro.com`
     - Service（服务）：`http://localhost:5002`（即本机 gunicorn）
     - 保存。
   - 若已有且指向 `localhost:5002`，跳过。
4. 回到隧道列表 → 点 `haiying-oa` → **Install（安装） / Connectors（连接器）**：
   - 选服务器系统（Linux），复制那行命令，形如：
     `cloudflared tunnel run --token eyJ............`
   - **整串 token 后面部署服务器时要用到**，先存到记事本。

> DNS 记录本身**不要动**（oa 已正确指向 haiying-oa 隧道）。

---

## 二、服务器侧（需要 SSH 登录到跑 OA 的机器）

下列命令在服务器上执行（Ubuntu / Debian 示例；CentOS 把 apt 换成 yum/dnf）。

### 2.1 上传代码
把整个 `haiyingtongbao/` 传到 `/var/www/haiyingtongbao/app`（scp / rsync / git）。
**不上传** `venv/`、`__pycache__/`、`.bat`。

### 2.2 建虚拟环境 + 装依赖
```bash
cd /var/www/haiyingtongbao/app
python3 -m venv venv
source venv/bin/activate
pip install -r deploy/requirements-prod.txt   # 含 gunicorn
```

### 2.3 初始化数据库（可重复跑，不破坏数据）
```bash
source venv/bin/activate
python seed.py
```

### 2.4 配置企业微信凭证（4 个环境变量）
方式 A（开机自启用）：编辑 `deploy/haiyingtongbao.service` 的 `Environment=` 行，
把 `<...>` 换成真实值，再 `systemctl daemon-reload`。
方式 B（临时测）：`export HYTB_WECOM_CORPID=...` 等四个（见 DEPLOY.md 第五章）。

### 2.5 启动 OA 程序（gunicorn，监听本机 5002）
```bash
sudo cp deploy/haiyingtongbao.service /etc/systemd/system/
# 编辑占位符（运行用户/企微4参）后：
sudo systemctl daemon-reload
sudo systemctl enable --now haiyingtongbao
```
自测：`curl -k https://127.0.0.1:5002/login` 应返回登录页 HTML。

### 2.6 安装 cloudflared（隧道连接器）
```bash
# 官方一键脚本（Linux）
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/bin/cloudflared
sudo chmod +x /usr/bin/cloudflared
cloudflared --version   # 能看到版本即成功
```

### 2.7 用 token 拉起隧道（最简，含凭证）
把第一步第 4 点复制的命令直接运行即可：
```bash
cloudflared tunnel run --token <粘贴 dashboard 给的 token>
```
能看到 `Registered tunnel connection` 之类日志即成功。此时浏览器开
`https://oa.crmwinpro.com` 应当能访问 OA（之前是 1033，现在正常）。

### 2.8 设为开机自启（生产必做）
```bash
sudo cp deploy/cloudflared/cloudflared.service /etc/systemd/system/
sudo nano /etc/systemd/system/cloudflared.service   # 把 token 粘进 ExecStart
sudo chmod 600 /etc/systemd/system/cloudflared.service
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared
```
查状态：`systemctl status cloudflared`
看日志：`journalctl -u cloudflared -f`

---

## 三、验证清单
1. 浏览器开 `https://oa.crmwinpro.com/login` → 能登录（admin / 123456，首次请改密码）。
2. 服务器重启后，`systemctl status haiyingtongbao cloudflared` 均应为 active(running)。
3. 企微「工作台」点 OA 应用 → 应免密进 OA（前提是企微后台可信域名/回调域已填 oa.crmwinpro.com）。

---

## 四、常见坑
- **仍报 1033**：隧道连接器没在跑（cloudflared 未启动 / token 过期）。`systemctl status cloudflared` 看报错；token 失效就在仪表盘重新复制。
- **502 / 空白页**：OA 程序没起（gunicorn 挂了）。`systemctl status haiyingtongbao` 看日志；确认监听 5002。
- **curl 127.0.0.1:5002 正常但外网 1033**：说明 cloudflared 没连上 Cloudflare，查服务器出网/防火墙是否禁了出站 443。
- **换服务器**：在原机器 `systemctl stop cloudflared` 后，新机器重跑 2.6–2.8 即可（token 可复用，或仪表盘吊销旧连接器）。

---

## 五、与旧方案（nginx + Let's Encrypt）的区别
本手册**不使用** `deploy/nginx_oa.crmwinpro.com.conf` 与 DEPLOY.md 里的 certbot / nginx 章节。
隧道模式下 TLS 在 Cloudflare 边缘终止，源站无证书、无公网端口暴露，更安全也更省事。
若将来改为"源站直接暴露公网 IP"的部署，才需要那份 nginx 配置。
