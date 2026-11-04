# 把「海盈通保OA管理系统」同步到 GitHub —— 操作指引（给威哥）

## 现状
代码已成功托管在 GitHub：
**https://github.com/minkinmao/HYTB-haiteng** （`main` 分支，仅含可部署代码，已自动排除 `docs/` 参考截图与保险协议原件）

> ⚠️ 关键限制：从**本机（Windows）和 WorkBuddy 环境都无法直连 github.com 的 git 协议**（会报 `Empty reply from server` / `502`）。所以普通的 `git push` 命令、GitHub Desktop 都用不了。我们改用 **GitHub REST API 推送**（`api.github.com` 能通），已验证可行。

## 以后要再同步代码（最简单，推荐）
直接跟 WorkBuddy 说"把最新代码推到 GitHub"，我会：
1. 让你去 GitHub 生成一个 classic token（`repo` 权限，7 天过期）；
2. 用脚本通过 API 把本地最新代码推上去（约 2–3 分钟，自动排除 `docs/`）；
3. 推完告诉你远程链接，并提醒你删掉那个 token。

你本机不需要装任何东西、不需要敲命令。

### 手动方式（备用，懂命令时自己跑）
1. GitHub → 右上角头像 → **Settings** → 左侧最底 **Developer settings** → **Personal access tokens** → **Tokens (classic)** → **Generate new token**，Note 随便写，Expiration 选 7 days，权限只勾 **`repo`**（整组），生成后复制那串 `ghp_xxx`。
2. 同步脚本已固化在项目根目录：`D:\workbuddy\海盈系统开发\_sync_to_github.py`（仓库外，不会被推上去）。运行：
   ```bash
   cd D:\workbuddy\海盈系统开发
   GITHUB_TOKEN='ghp_xxx' python3 _sync_to_github.py
   ```
   脚本会推送可部署代码（排除 `docs/`），并在远程 `main` 上**叠加一个新提交**（保留连续历史，第三方 `git pull` 正常）。远程永远只含可部署文件、不含 `docs/`。

## 安全提醒（每次推送前都看）
- 仓库里**不应出现** `wecom_config.txt`（真实企微密钥）、`haiying.db`、任何 `.bak` 文件、`venv/` 目录。这些已被 `.gitignore` 屏蔽，正常推送不会带上；**千万不要用 `git add -f` 强制加**，否则会泄露。
- token 用完即删（Settings → Developer settings → 删掉那条 token）。
- 你本机的 git 提交历史**完整保留**，与远程无关；远程由 API 脚本每次叠加一个新提交生成，保持连续历史，方便第三方 `git pull` 拉取更新。

## 怎么交给第三方部署
1. 让第三方注册一个 GitHub 账号，你在 `HYTB-haiteng` 仓库 `Settings → Collaborators` 邀请他（**私有仓库 + 邀请最安全**）；或临时设为 Public（密钥已排除，代码公开不泄露，但业务逻辑会暴露，仍建议私有 + 邀请）。
2. 第三方按仓库内的 **`部署指南_第三方.md`** 操作：
   克隆 → 建 venv 装依赖 → 配企微凭证 → `python seed.py` 初始化空库 → gunicorn + nginx + systemd 上线 → 绑二级域名 + HTTPS。
3. 不想给 GitHub 权限也可以：在 GitHub 打一个 Release，下载 ZIP 发对方（部署指南同样在 ZIP 内）。

## 备注
- 本项目的 `.gitignore` 会忽略 `*.bat`（Windows 启动脚本）、保留 `.sh`（Linux 启动脚本）。若你日后从 GitHub 克隆回 Windows 缺 `.bat`，告诉我重新生成即可。
- 企微凭证由第三方在**自己服务器**上用环境变量填（`HYTB_WECOM_*`），不要落地进仓库、也不要发给我。
- 第三方部署相关完整文档见同目录 `部署指南_第三方.md`（已随仓库一起推送，对方 clone 即得）。

---

## 日常维护流程（改代码 → 自测 → 提交 → 同步 → 通知第三方）

以后你要改功能、修 bug，按下面 5 步走，全程**不要**用 `git push` / `git pull` 直接碰 GitHub（git 协议连不上，且本机历史与远程是分开的）。

### 1. 改代码（在你本机）
项目目录：
`D:\workbuddy\海盈系统开发\HY-OA系统开发\haiyingtongbao`
用 VS Code 或 WorkBuddy 编辑器改里面的文件（Flask + Pandas + JS + ECharts，别动整体架构）。

### 2. 本机自测
用本机托管 Python 启动（已装好依赖、在 PATH）：
```bash
cd D:\workbuddy\海盈系统开发\HY-OA系统开发\haiyingtongbao
python app.py
```
浏览器开 `http://localhost:5000` 看效果，确认没问题再往下。

### 3. 本地提交
```bash
git add -A && git commit -m "这里写改了啥"
```

### 4. 同步到 GitHub（让我跑脚本）
把令牌（`ghp_` 开头、repo 权限）发给我，我执行：
```bash
GITHUB_TOKEN=你的令牌 python3 D:/workbuddy/海盈系统开发/_sync_to_github.py
```
约 2–3 分钟推完，只推可部署代码（自动排除 `docs/`）。推完我提醒你删令牌。

### 5. 通知第三方更新
你改完同步后，让第三方在阿里云服务器上拉最新并重启：
```bash
cd /path/to/haiyingtongbao
git pull
systemctl restart haiyingtongbao   # 或他实际用的服务名
```
（远程历史是连续的，他那边 `git pull` 正常，不会冲突。）

### ⚠️ 三个必须记住的坑
1. **本机别用 `git push` / `git pull` 碰 GitHub**——git 协议连不上 github.com（报 `Empty reply`），且本机历史与远程分开，硬 pull/push 会乱。远程只通过第 4 步的脚本更新。
2. **`docs/` 永不推送**——里面是保险协议原件和截图，含敏感商业信息，脚本已自动排除。
3. **令牌用完即删**——每次给我的是 7 天有效令牌，推完去 GitHub → Settings → Developer settings → PAT 删掉，下次再生成新的。

### 文件位置速查
- 本地项目：`D:\workbuddy\海盈系统开发\HY-OA系统开发\haiyingtongbao`
- 同步脚本（仓库外，不被推）：`D:\workbuddy\海盈系统开发\_sync_to_github.py`
- 远程仓库：`https://github.com/minkinmao/HYTB-haiteng`（`main` 分支）
- 第三方部署文档（在仓库内）：`部署指南_第三方.md`
