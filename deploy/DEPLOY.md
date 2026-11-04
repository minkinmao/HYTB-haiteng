# 海盈通保OA管理系统 · 生产部署清单

> 目标：把本机开发好的系统部署到公网，接上企业微信（先上 OA）。
> 适用对象：非专业运维，照步骤做即可。所有 `<...>` 都是要你替换的占位符。
> 前置结论：**企业微信强制要求 HTTPS + 已备案域名**，所以"公网 + 域名 + 证书"是硬门槛，必须先有。

---

## 一、前置条件（先确认，否则后面全卡住）

| 条件 | 说明 |
|------|------|
| 一台云服务器 | 推荐 Linux（Ubuntu 22.04 / CentOS 7+），2核4G 起步。企微也支持 Windows Server，但本文主推 Linux。 |
| 公网 IP | 云厂商分配，安全组放行 **80、443** 端口（企微校验和回调用）。 |
| 一个域名 | 如 `oa.haiyingtongbao.com`，已完成 **ICP 备案**（国内服务器必需）。 |
| HTTPS 证书 | 用 Let's Encrypt 免费证书（certbot 自动申请+续期），下文有命令。 |
| Python | 服务器装 Python 3.10+（本机项目按 3.10+ 验证）。 |

> 如果还没域名/备案，先去云厂商或域名商办。这是企微接入的硬约束，代码解决不了。

---

## 二、上传代码到服务器

把整个 `haiyingtongbao/` 目录上传到服务器，建议路径：

```
/var/www/haiyingtongbao/app
```

上传方式任选（scp / rsync / 宝塔 / git clone）。**不要上传** `venv/`、`__pycache__/`、`.bat` 文件（Windows 专用，服务器用不上）。

---

## 三、建虚拟环境 + 装依赖

```bash
cd /var/www/haiyingtongbao/app
python3 -m venv venv
source venv/bin/activate
pip install -r deploy/requirements-prod.txt   # 含 gunicorn
```

---

## 四、初始化数据库（可重复执行，不破坏已有数据）

```bash
source venv/bin/activate
python seed.py     # 与开发机双击「启动系统.bat」第3步等价，会自动加表加列
```

---

## 五、配置企业微信凭证（4 个环境变量）

**凭证绝不下发前端、绝不写进代码**，只放在服务器环境变量里。
两种方式任选其一：

**方式 A（推荐，开机自启用）**：写进 `deploy/haiyingtongbao.service` 的 `Environment=` 行，
把 `<你的企业微信 CorpID>` 等换成真实值，然后 `systemctl daemon-reload`。

**方式 B（临时测试用）**：启动前 export：

```bash
export HYTB_WECOM_CORPID="wwxxxxxxxx"
export HYTB_WECOM_AGENTID="1000002"
export HYTB_WECOM_SECRET="xxxxxxxxxxxxxxxx"
export HYTB_WECOM_DOMAIN="https://oa.haiyingtongbao.com"
```

四个值从企微后台拿（「我的企业」→CorpID；「应用管理」→你建的应用→AgentId/Secret）。

---

## 六、申请 HTTPS 证书（Let's Encrypt，免费）

```bash
# 安装 certbot（Ubuntu 示例）
sudo apt update && sudo apt install -y certbot python3-certbot-nginx

# 申请证书（按提示填邮箱，选不重定向，nginx 配置我们手动改）
sudo certbot certonly --nginx -d oa.haiyingtongbao.com
```

证书生成在 `/etc/letsencrypt/live/oa.haiyingtongbao.com/`，
`nginx_haiyingtongbao.conf` 里的 `ssl_certificate` 路径正是指向这里（记得把 `<你的域名>` 换成真实域名）。

自动续期测试：`sudo certbot renew --dry-run`

---

## 七、放 nginx 配置 + 企微校验文件

1. 把 `deploy/nginx_haiyingtongbao.conf` 复制到 `/etc/nginx/conf.d/haiyingtongbao.conf`，
   把里面所有 `<你的域名>` 换成真实域名。
2. 企微「可信域名」校验文件：
   - 在企微后台「应用管理→网页授权及JS-SDK→可信域名」下载 `WW_verify_XXXX.txt`
   - 服务器上建目录 `sudo mkdir -p /var/www/wecom_verify`，把文件传进去
3. 检查并重载：
   ```bash
   sudo nginx -t          # 语法检查，看到 successful 才继续
   sudo systemctl reload nginx
   ```

---

## 八、配置开机自启（systemd，推荐生产用）

```bash
sudo cp deploy/haiyingtongbao.service /etc/systemd/system/
# 编辑文件，把 <运行用户> / <路径> / 4个凭证 占位符换成真实值
sudo nano /etc/systemd/system/haiyingtongbao.service
sudo systemctl daemon-reload
sudo systemctl enable --now haiyingtongbao
```

查状态：`systemctl status haiyingtongbao`
看日志：`journalctl -u haiyingtongbao -f`

> 不想配 systemd 就用 `bash deploy/start_prod.sh` 简易后台启动（停：`pkill -f haiyingtongbao`）。

---

## 九、启动验证

1. 本机自测：`curl -k https://127.0.0.1:5002/login` 应返回登录页 HTML。
2. 浏览器开 `https://你的域名/login`，应能正常登录（admin / 123456，首次请改密码）。
3. PC 端功能通了，**再测企微**：手机企微「工作台」点你建的应用 → 应免密自动进 OA。
4. 企微进不去时排查顺序：
   - 域名 HTTPS 是否通（浏览器能开）
   - 企微后台「可信域名」校验文件是否放对（访问 `http://域名/WW_verify_xxx.txt` 能看到内容）
   - 「网页授权回调域」是否填了你的域名（不带 https）
   - 「企业可信IP」是否填了服务器出口 IP
   - 4 个环境变量是否真生效（`journalctl -u haiyingtongbao` 看报错）

---

## 十、企业微信后台对接要点（回顾）

| 配置项 | 填什么 |
|--------|--------|
| 应用主页（网页应用） | `https://你的域名/wecom/entry` |
| 可信域名 | 你的域名（放 WW_verify 校验文件） |
| 网页授权回调域 | 你的域名（不带 https） |
| 企业可信IP | 服务器公网出口 IP |
| 可见范围 | 那 8 类内部管理人员 |

账号绑定（管理员 Web 操作）：登录 PC 端 → IT 用户管理 → 给每个员工填「企微 userid」（企微通讯录里的账号，非昵称）→ 保存。

---

## 十一、若服务器是 Windows（备选方案）

gunicorn 只支持 Linux。若生产服务器是 Windows Server：

1. 用 **waitress** 替代 gunicorn（纯 Python，Windows 可用）：
   `pip install waitress`，然后启动：`waitress-serve --port=5002 --call app:app`
2. Windows 版 nginx 同样可用（配置同上，路径改 Windows 风格如 `C:/www/wecom_verify`）。
3. 凭证用「系统环境变量」设置（控制面板→高级→环境变量），或写个 `start_prod.bat` 里 `set HYTB_WECOM_...=...` 再启动。
4. 开机自启用「任务计划程序」配一个开机触发的启动任务，替代 systemd。

---

## 十二、常见坑

- **证书路径报错**：nginx 启动时若 `/etc/letsencrypt/...` 不存在，先跑第六步 certbot。
- **静态资源 404**：若启用了 nginx 的 `/static` 段，alias 路径要和实际一致；拿不准就注释掉那段。
- **上传附件失败**：nginx 默认 `client_max_body_size 1m`，已在本配置设为 50m；若仍失败调大。
- **企微一直跳登录页**：多半是 4 个环境变量没生效，或 userid 没绑定对（查 User 表的 wecom_userid）。
- **debug 模式别上生产**：本配置用 gunicorn 启动，不会进 debug；切勿 `python run.py` 当生产服务。
