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

### 手动方式（备用，仅当懂命令时）
1. GitHub → 右上角头像 → **Settings** → 左侧最底 **Developer settings** → **Personal access tokens** → **Tokens (classic)** → **Generate new token**，Note 随便写，Expiration 选 7 days，权限只勾 **`repo`**（整组），生成后复制那串 `ghp_xxx`。
2. 把同步脚本（`_api_sync.py`，与 `haiyingtongbao` 同级目录）保存好，运行：
   ```bash
   cd D:\workbuddy\海盈系统开发
   GITHUB_TOKEN='ghp_xxx' python3 _api_sync.py
   ```
   脚本会推送可部署代码（排除 `docs/`），并以**干净提交覆盖 `main`**，远程不会残留 `docs/` 与历史大文件。

## 安全提醒（每次推送前都看）
- 仓库里**不应出现** `wecom_config.txt`（真实企微密钥）、`haiying.db`、任何 `.bak` 文件、`venv/` 目录。这些已被 `.gitignore` 屏蔽，正常推送不会带上；**千万不要用 `git add -f` 强制加**，否则会泄露。
- token 用完即删（Settings → Developer settings → 删掉那条 token）。
- 你本机的 4 个提交历史**完整保留**，远程只是一份干净的部署快照（这是 API 推送的固有限制）。

## 怎么交给第三方部署
1. 让第三方注册一个 GitHub 账号，你在 `HYTB-haiteng` 仓库 `Settings → Collaborators` 邀请他（**私有仓库 + 邀请最安全**）；或临时设为 Public（密钥已排除，代码公开不泄露，但业务逻辑会暴露，仍建议私有 + 邀请）。
2. 第三方按仓库内的 **`部署指南_第三方.md`** 操作：
   克隆 → 建 venv 装依赖 → 配企微凭证 → `python seed.py` 初始化空库 → gunicorn + nginx + systemd 上线 → 绑二级域名 + HTTPS。
3. 不想给 GitHub 权限也可以：在 GitHub 打一个 Release，下载 ZIP 发对方（部署指南同样在 ZIP 内）。

## 备注
- 本项目的 `.gitignore` 会忽略 `*.bat`（Windows 启动脚本）、保留 `.sh`（Linux 启动脚本）。若你日后从 GitHub 克隆回 Windows 缺 `.bat`，告诉我重新生成即可。
- 企微凭证由第三方在**自己服务器**上用环境变量填（`HYTB_WECOM_*`），不要落地进仓库、也不要发给我。
- 第三方部署相关完整文档见同目录 `部署指南_第三方.md`（已随仓库一起推送，对方 clone 即得）。
