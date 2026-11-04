# 把「海盈通保OA管理系统」同步到 GitHub —— 操作指引（给威哥）

## 现状
代码已成功托管在 GitHub：
**https://github.com/minkinmao/HYTB-haiteng** （`main` 分支，仅含可部署代码，已自动排除 `docs/` 参考截图与保险协议原件）

> ⚠️ 关键限制：从**本机（Windows）和 WorkBuddy 环境都无法直连 github.com 的 git 协议**（会报 `Empty reply from server` / `502`）。所以普通的 `git push` 命令、GitHub Desktop 都用不了。我们改用 **GitHub REST API 推送**（`api.github.com` 能通），已验证可行。

## 令牌怎么设置（只需设置一次，之后全自动）
为避免每次都要重新生成/粘贴令牌，脚本现在会**自动读取本机保存的令牌文件**，你只需设置一次。

### 第 1 步：在 GitHub 生成令牌（只做一次）
1. 浏览器登录 GitHub → 右上角头像 → **Settings**；
2. 左侧最底部 **Developer settings** → **Personal access tokens** → **Tokens (classic)**；
3. 点 **Generate new token (classic)**，Note 随便写（如 `haiying-oa-sync`）；
4. Expiration（有效期）：想省事就选 **No expiration**（不过期）；想更安全可选 90 days，到期再换；
5. 权限只勾 **`repo`**（整组）——这是同步代码必需的最小权限；
6. 点底部 **Generate token**，复制那串 `ghp_` 开头的令牌（**只显示一次，务必复制**）。

> 更安全的可选做法：用 **Fine-grained tokens**，只授权 `minkinmao/HYTB-haiteng` 这一个仓库的 `Contents: Read and write` 权限。步骤略多，但泄露风险更小。小白建议先用 classic。

### 第 2 步：把令牌保存到本机（只做一次）
双击项目外层的 **`设置GitHub令牌.bat`**（`D:\workbuddy\海盈系统开发\设置GitHub令牌.bat`），按提示粘贴令牌并回车。它会写到：
`D:\workbuddy\海盈系统开发\_github_token.txt`（**仓库外、不会被推到 GitHub**）。

保存后，脚本以后每次自动读取，**不需要再输入令牌**。

### 第 3 步：以后同步代码（两种方式）
- **方式 A（推荐，跟我说一声）**：直接说"把最新代码推到 GitHub"，我跑脚本，全程不用你再贴令牌（我读本机那个文件）。
- **方式 B（自己点）**：双击 **`同步到GitHub.bat`**，自动同步，结束后窗口会停在结果页。

两种方式都会：只推可部署代码（自动排除 `docs/`）、在远程 `main` 上**叠加一个新提交**（保留连续历史，第三方 `git pull` 正常）。

### 手动方式（懂命令时自己跑）
```bash
cd D:\workbuddy\海盈系统开发
GITHUB_TOKEN='ghp_xxx'  python3 _sync_to_github.py   # 临时用环境变量（会覆盖本机令牌文件）
# 或不带环境变量，脚本自动读 _github_token.txt：
python3 _sync_to_github.py
```

## 安全提醒（每次推送前都看）
- 仓库里**不应出现** `wecom_config.txt`（真实企微密钥）、`haiying.db`、任何 `.bak` 文件、`venv/` 目录。这些已被 `.gitignore` 屏蔽，正常推送不会带上；**千万不要用 `git add -f` 强制加**，否则会泄露。
- 令牌保存在本机 `_github_token.txt`（**仓库外**，不会被推）；不是用完就删。若怀疑泄露，去 GitHub → Settings → Developer settings 删除该令牌即可**立即失效**，再重新生成、重新双击「设置GitHub令牌.bat」保存一次即可。
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

### 4. 同步到 GitHub（令牌已存本机，直接跑）
令牌已保存在 `_github_token.txt`（仓库外），**不需要再提供令牌**。直接：
- 跟 WorkBuddy 说"同步到 GitHub"，我跑脚本；或
- 双击 `同步到GitHub.bat` 自己跑。

约 2–3 分钟推完，只推可部署代码（自动排除 `docs/`）。

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
3. **令牌只需设置一次**——保存在本机 `D:\workbuddy\海盈系统开发\_github_token.txt`（仓库外、不入库），脚本以后自动读取，不用每次再贴。怀疑泄露时，去 GitHub 删掉该令牌即可立即失效。

### 文件位置速查
- 本地项目：`D:\workbuddy\海盈系统开发\HY-OA系统开发\haiyingtongbao`
- 同步脚本（仓库外，不被推）：`D:\workbuddy\海盈系统开发\_sync_to_github.py`
- 令牌文件（仓库外，不入库）：`D:\workbuddy\海盈系统开发\_github_token.txt`
- 设置令牌（双击一次）：`D:\workbuddy\海盈系统开发\设置GitHub令牌.bat`
- 一键同步（双击）：`D:\workbuddy\海盈系统开发\同步到GitHub.bat`
- 远程仓库：`https://github.com/minkinmao/HYTB-haiteng`（`main` 分支）
- 第三方部署文档（在仓库内）：`部署指南_第三方.md`
