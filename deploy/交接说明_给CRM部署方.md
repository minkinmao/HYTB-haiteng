# 交接说明：把「海盈通保OA管理系统」挂到 oa.crmwinpro.com

> 发给：当初部署 crmwinpro.com / CRM 系统的同事或供应商
> 目的：让二级域名 **oa.crmwinpro.com** 对外提供「海盈通保OA管理系统」（Flask 内网管理后台）

---

## 一、现状（你们之前应该已经铺过一半）

在 Cloudflare 的 **crmwinpro.com** 域名 DNS 里，已经有这两条**隧道(Tunnel)**记录：

| 名称 | 类型 | 内容 | 代理 |
|------|------|------|------|
| crmwinpro.com | 隧道 | `crmwinpro` | 已代理 |
| oa.crmwinpro.com | 隧道 | `haiying-oa` | 已代理 |

也就是说 **`oa` 的 DNS 已经指向隧道 `haiying-oa`，无需改 DNS**。
当前访问 `https://oa.crmwinpro.com` 报 **1033 / error 1033**，原因只有一个：
**`haiying-oa` 这条隧道的连接器(cloudflared)没有在服务器上跑起来**，所以 Cloudflare 找不到源站。

---

## 二、需要对方完成的两块工作

### A. Cloudflare 仪表盘（Zero Trust）
1. 进 **Zero Trust → Networks(网络) → Tunnels(隧道)**，找到 **`haiying-oa`**。
2. 确认它有一条 **Public Hostname(公共主机名)**：`oa.crmwinpro.com` → `http://localhost:5002`。
   - 没有就加一条（Subdomain=`oa`，Domain=`crmwinpro.com`，Service=`http://localhost:5002`）。
3. 在该隧道的 **Install / Connectors** 里复制 `cloudflared tunnel run --token <一串>` 命令（含凭证），后面服务器要用。

### B. 服务器（跑 OA 程序的那台）
> 代码就是「海盈通保OA」项目，部署文件都在 `deploy/` 目录下，详见 **`deploy/DEPLOY_CLOUDFLARE_TUNNEL.md`**（逐条命令版）。

1. 把项目传到服务器，建议 `/var/www/haiyingtongbao/app`。
2. 建 venv、装依赖：`pip install -r deploy/requirements-prod.txt`（含 gunicorn）。
3. 初始化库：`python seed.py`（可重复执行，不破坏数据）。
4. 配企业微信 4 个环境变量（如暂不需要企微可后补）：
   `HYTB_WECOM_CORPID` / `HYTB_WECOM_AGENTID` / `HYTB_WECOM_SECRET` / `HYTB_WECOM_DOMAIN=https://oa.crmwinpro.com`
5. 起 OA 程序（gunicorn，监听本机 5002）：
   `sudo cp deploy/haiyingtongbao.service /etc/systemd/system/` → 填好占位符 →
   `systemctl daemon-reload && systemctl enable --now haiyingtongbao`
   - 应用入口是 `app:app`，gunicorn 配置 `deploy/gunicorn.conf.py` 已绑定 `127.0.0.1:5002`。
6. 装 cloudflared，用第 A.3 步的 token 拉起隧道：
   `cloudflared tunnel run --token <token>`
   生产自启用 `deploy/cloudflared/cloudflared.service`（把 token 粘进 `ExecStart`，`systemctl enable --now cloudflared`）。

---

## 三、⚠️ 端口冲突提醒（重要）
隧道设定 OA 走本机 **5002**。如果 OA 和 CRM 跑在**同一台服务器**，而 CRM 的 gunicorn 也已占 5002，就会冲突。
- 解决：OA 改用别的端口（如 `5003`），改两处：
  1. `deploy/gunicorn.conf.py` 的 `bind = "127.0.0.1:5003"`
  2. Cloudflare 隧道 Public Hostname 的 Service 改成 `http://localhost:5003`
- 若 OA 跑在**独立新服务器**，则用 5002 即可，和 CRM 互不影响。

---

## 四、完成标准（验收）
- 浏览器开 `https://oa.crmwinpro.com/login` → 能显示 OA 登录页、能用 admin / 123456 登录（首次请改密码）。
- 服务器重启后 `systemctl status haiyingtongbao cloudflared` 均为 `active (running)`。

---

## 五、对方可能要问的（背景）
- **这是什么系统**：海盈通保的内部 OA 管理系统（Flask + SQLite），仅 8 类内部管理人员使用，不含保单/资金交易。
- **数据库**：SQLite（`haiying.db`），随项目走，无需单独装数据库。
- **为什么用 Tunnel 而不是 nginx+证书**：因为 crmwinpro.com 本身已是 Cloudflare Tunnel 架构，oa 延续同一套，源站无需公网 IP/开端口/证书，更省事也更安全。
- **详细命令**：见同目录 `DEPLOY_CLOUDFLARE_TUNNEL.md`；原有 nginx 版部署说明（`DEPLOY.md`）本次**不适用**。
