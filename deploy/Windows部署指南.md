# 海盈通保 OA · Windows 部署手册（oa.crmwinpro.com）

> 目标：把 OA 系统通过二级域名 `oa.crmwinpro.com` 发布到公网。
> 架构：**Windows 本机跑 OA（127.0.0.1:5002） + Cloudflare Tunnel（`haiying-oa`）反向代理到公网**，
> 不需要公网 IP、不开 80/443 端口、不申请证书、不配 nginx/IIS。
> 前提：域名 `crmwinpro.com` 已在 Cloudflare（账号 `minkinmao@qq.com`），DNS 已由 Cloudflare 托管。

---

## 〇、整体流量走向

```
外网用户浏览器
      │  https://oa.crmwinpro.com
      ▼
Cloudflare 边缘（TLS 在此终止，自动 HTTPS）
      │  加密隧道（出站连接，不开入站端口）
      ▼
Windows 本机 cloudflared（隧道 haiying-oa）
      │
      ▼
127.0.0.1:5002  ——  OA Flask 进程（venv\Scripts\python.exe run.py）
```

同机还跑着 CRM（`crmwinpro.com` → `127.0.0.1:5001`），用的是另一条托管隧道 `Cloudflared` 服务（token 模式），**与 OA 的 `haiying-oa` 隧道互不影响**。

---

## 一、关键路径与账号一览

| 项 | 值 |
|----|----|
| 代码目录 | `D:\workbuddy\海盈系统开发\HY-OA系统开发\haiyingtongbao` |
| Python 解释器 | `venv\Scripts\python.exe`（Python 3.13） |
| OA 监听 | `127.0.0.1:5002`（仅本机，外网不可直连） |
| cloudflared | `D:\workbuddy\营销数据查询\cloudflared.exe` |
| OA 隧道配置 | `C:\Users\Administrator\.cloudflared\haiying-oa.yml` |
| OA 隧道凭证 | `C:\Users\Administrator\.cloudflared\10cfcb8f-8319-4e67-8a3c-8ddfa9d63f91.json` |
| OA 隧道 ID | `10cfcb8f-8319-4e67-8a3c-8ddfa9d63f91`（名称 `haiying-oa`） |
| CRM 隧道（对照） | `9ae8a68c-5f78-4430-aac3-6ceb39487d5f`（名称 `crmwinpro`，token 托管） |
| 访问地址 | `https://oa.crmwinpro.com/login` |

---

## 二、获取代码

```powershell
git clone https://github.com/minkinmao/haiyingtongbao.git
cd haiyingtongbao
```

> 仓库已排除：`venv/`、`*.db`、`*.bak`、`wecom_config.txt`（含企微密钥，仅本机保留）。

---

## 三、装依赖

```powershell
cd D:\workbuddy\海盈系统开发\HY-OA系统开发\haiyingtongbao
python -m venv venv
venv\Scripts\pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

---

## 四、配置企业微信凭证

企微凭证**只走环境变量或本机 `wecom_config.txt`**，绝不写进代码、绝不进 Git 仓库。
`wecom.py` 的取值优先级：`HYTB_WECOM_*` 环境变量 → `wecom_config.txt`。

本机 `wecom_config.txt` 示例（**此文件已在 `.gitignore` 中，不会上传**）：

```ini
CORPID=你的企业ID
AGENTID=你的应用AgentId
SECRET=你的应用Secret
DOMAIN=https://oa.crmwinpro.com
```

> ⚠️ `DOMAIN` 必须是 `https://oa.crmwinpro.com`（不带尾斜杠）。改完要**重启 OA 进程**才生效。

---

## 五、本机自测

```powershell
venv\Scripts\python.exe run.py
# 另开窗口：
curl http://127.0.0.1:5002/login
```

看到登录页 HTML 即 OK。浏览器开 `http://127.0.0.1:5002/login`，默认账号 `admin / 123456`（**首次登录请立刻改密码**）。

---

## 六、Cloudflare Tunnel（关键）

### 6.1 隧道配置文件

`C:\Users\Administrator\.cloudflared\haiying-oa.yml`：

```yaml
tunnel: 10cfcb8f-8319-4e67-8a3c-8ddfa9d63f91
credentials-file: C:\Users\Administrator\.cloudflared\10cfcb8f-8319-4e67-8a3c-8ddfa9d63f91.json
# 国内部分 ISP 对 QUIC 不稳，强制 http2（与 CRM 隧道保持一致）
protocol: http2

ingress:
  - hostname: oa.crmwinpro.com
    service: http://127.0.0.1:5002
  - service: http_status:404
```

### 6.2 手动试跑隧道

```powershell
D:\workbuddy\营销数据查询\cloudflared.exe --config C:\Users\Administrator\.cloudflared\haiying-oa.yml tunnel run haiying-oa
```

日志应出现 `Registered tunnel connection`，预检全部 PASS。

### 6.3 绑定二级域名（只需一次）

```powershell
# 已绑定会提示 "already configured to route to your tunnel"
D:\workbuddy\营销数据查询\cloudflared.exe tunnel route dns haiying-oa oa.crmwinpro.com
```

> ⚠️ 最容易踩坑的点：`oa.crmwinpro.com` 必须指向 **haiying-oa** 隧道（`10cfcb8f...`），
> 不能指向 CRM 那条 `crmwinpro` 隧道（`9ae8a68c...`）。用上面命令核对最省事。

---

## 七、开机自启（Windows 计划任务）

本机已创建两个计划任务（**开机自动启动，无需人工登录**）：

| 任务名 | 作用 | 启动命令 |
|--------|------|----------|
| `HaiyingOA-App` | 跑 OA 进程 | `venv\Scripts\python.exe run.py`（工作目录=代码目录） |
| `HaiyingOA-Tunnel` | 跑 OA 隧道 | `cloudflared.exe --config ...\haiying-oa.yml tunnel run haiying-oa` |

管理命令：

```powershell
# 查看状态
Get-ScheduledTask -TaskName "HaiyingOA-*" | Select-Object TaskName,State

# 手动启动
Start-ScheduledTask -TaskName "HaiyingOA-App"
Start-ScheduledTask -TaskName "HaiyingOA-Tunnel"

# 手动停止
Stop-ScheduledTask -TaskName "HaiyingOA-App"
Stop-ScheduledTask -TaskName "HaiyingOA-Tunnel"
```

> 为什么用计划任务而不是 Windows 服务？
> 本机已有一个名为 `Cloudflared` 的服务（token 托管模式跑 CRM 隧道），
> 再装同名服务会冲突。计划任务互不干扰，且支持失败自动重启（1 分钟重试 3 次）。

---

## 八、验证清单

1. 浏览器开 `https://oa.crmwinpro.com/login` → 能看到登录页（`海盈通保OA管理系统`）。
2. 用 `admin / 123456` 登录成功（首次请改密码）。
3. 重启 Windows 后，两个计划任务自动起来：
   ```powershell
   Get-ScheduledTask -TaskName "HaiyingOA-*"
   ```
4. 企微「工作台」点 OA 应用 → 免密进 OA
   （前提是企微后台可信域名/回调域填 `oa.crmwinpro.com`）。

---

## 九、常见坑速查

| 现象 | 原因 | 处理 |
|------|------|------|
| `error code: 1033` / 530 | 隧道没在跑，或 hostname 未配置 | `Start-ScheduledTask -TaskName "HaiyingOA-Tunnel"`；核对 ingress |
| 502 / 空白页 | OA 进程没起 | `Start-ScheduledTask -TaskName "HaiyingOA-App"`；看 5002 是否监听 |
| CNAME 指错隧道 | 指到了 CRM 隧道 | `cloudflared tunnel route dns haiying-oa oa.crmwinpro.com` |
| 企微跳登录页 | `DOMAIN` 还是旧地址 / 凭证没生效 | 检查 `wecom_config.txt` 的 `DOMAIN`，重启 OA |
| 隧道进程查询时串到 CRM | cloudflared 自动继承 `~/.cloudflared/config.yml` 的 `tunnel` 字段 | 用 `--config` 显式指定 `haiying-oa.yml`，或用 UUID 查询 |
| OA 只能本机访问 | 正常！gunicorn/Flask 只监听 `127.0.0.1` | 外网必须走隧道，不要改成 `0.0.0.0` |

---

## 十、企微后台对接要点

| 配置项 | 填什么 |
|--------|--------|
| 应用主页 | `https://oa.crmwinpro.com/wecom/entry` |
| 可信域名 | `oa.crmwinpro.com`（放 `WW_verify_*.txt`） |
| 网页授权回调域 | `oa.crmwinpro.com`（不带 https） |
| 企业可信IP | 不用填（隧道模式源站无公网 IP，企微走 CF 回源） |
| 可见范围 | 内部管理人员 |

---

## 十一、安全约定

1. **`wecom_config.txt` 永不进 Git**（含企微 Secret 明文）——已在 `.gitignore`。
2. **部署文档里只写占位符**，绝不写真实 Secret/CORPID。
3. **`run.py` 必须 `debug=False`**：公网暴露下 Werkzeug 调试器有被远程利用的风险。
4. OA 只监听 `127.0.0.1:5002`，**不要**改成 `0.0.0.0`（会绕过隧道直接暴露）。
5. GitHub 推送用完 PAT 立即在 GitHub 后台撤销令牌。
