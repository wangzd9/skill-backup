---
name: git-github-mirror-remote
description: Use when 给 GitLab 仓库加 GitHub 镜像 remote 并 push。
---

# 内网 GitLab 仓库关联 GitHub 并推送

## When to Use

用户要求把本地从内网 GitLab 拉取的仓库关联到 GitHub 空仓库并 push，且明确要求"不动原 GitLab 连接，新增 github"。

## 环境事实（开发机）

- `~/.ssh/config` 已配 `Host github.com` → `IdentityFile ~/.ssh/id_ed25519_github_v2`（**无口令**钥，当前有效）、`IdentitiesOnly yes`。
- 旧钥 `id_ed25519_github` 是**口令加密**的，非图形会话（Hermes terminal/cron/user service）无 agent 可签名时必然被拒；它留在磁盘和 GitHub 上但**不是可用通道**，诊断时看到 `IdentitiesOnly` 指向它或回退到它是死路，直接用 v2。
- 无 `gh` CLI，无 `~/.git-credentials`，`credential.helper=cache` 通常无缓存条目——HTTPS push 直接 `could not read Username`。

## 步骤

1. **预检**：`git remote -v && git status --short`——记下 origin 现有指向（不得改动），确认工作区无未预期改动。
2. **新增 remote**：`git remote add github <仓库 URL>`。若用户给的是 HTTPS URL 可先照录。
3. **认证预检**：`ssh -o BatchMode=yes -T git@github.com` 应返回 `Hi <user>!` 认证成功。
4. **push**：先试 `git push -u github <主分支>`；HTTPS 报 `could not read Username` 即换 SSH：`git remote set-url github git@github.com:<user>/<repo>.git` 后重推。
5. **核验（必做）**：`git remote -v` 确认 origin 仍指 GitLab；`git fetch github && git log --oneline -1 github/<分支>` 与本地 HEAD 同哈希。

## push 失败诊断（Permission denied / 404 时）

认证预检（步骤 3）不通过时，按层隔离再下结论，不要直接报"连不上 GitHub"：

1. 端口：`timeout 10 bash -c 'echo > /dev/tcp/github.com/22'`（本机 22/443 均可达，网络层基本不是问题）。
2. remote/分支：`git remote -v && git status -sb`——确认 URL 与 ahead/behind 数字，push 被拒前先看领先多少提交。
3. 认证：`ssh -T -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes -o BatchMode=yes git@github.com` 必须返回 `Hi <user>!`。
4. 账号与仓库存在性：`curl -s -o /dev/null -w '%{http_code}' https://github.com/<user>` 和 `https://github.com/<user>/<repo>`——仓库 404 说明已删/改名/私有三者之一；**未认证时无法区分私有与不存在**，本机无 gh CLI/无 API token，不要拿 api.github.com 的 401/404 当判据，让用户登录网页确认。
5. 若 `ssh -v -T` 出现 `Server accepts key` 却最终 `Permission denied (publickey)`：这只证明 GitHub **认识**该公钥（绑在某账号下），拒签还有两个独立症结，必须先分清再让用户核对账号：
   - **(a) key 文件加密 + 当前会话无 agent 可签名（最常见，易误判成账号问题）**：`ssh-keygen -y -P '' -f <IdentityFile>` 返回 `incorrect passphrase` 即 key 加密。非图形会话（Hermes terminal、cron、user service）常常没有 `SSH_AUTH_SOCK` 或 gnome-keyring 已锁，ssh 无法交互输口令，BatchMode 下签名静默失败。对照验证：`ssh-add -l`（agent 里有已解锁的 key）+ `env -u SSH_AUTH_SOCK ssh -T git@github.com`（无 agent 必失败）——同一把 key 两条结果相反即坐实 (a)。`~/.ssh/config` 的 `IdentitiesOnly yes` 会强制只读 key 文件、禁用 agent 兜底，放大此问题。根治：生成**无口令**新钥 `ssh-keygen -t ed25519 -N '' -f <key>_v2`，公钥加进 GitHub，`~/.ssh/config` 的 `IdentityFile` 切到 v2，用 `env -u SSH_AUTH_SOCK` 场景实测 push 验证。旧口令找不回来不要找（按安全边界从未入库）,直接换新钥即可。
   - **"四小时前还能推"类矛盾**：成功与失败可能走的是不同签名通道——图形/登录会话有 gnome-keyring ssh-agent 缓存已解锁钥，cron/终端会话没有。诊断先 `git reflog <remote>/<branch> --date=iso` 找出成功 push 的时间点，再查该时刻持 agent 套接字的进程环境（`/proc/<pid>/environ` 的 `SSH_AUTH_SOCK`）与当前会话的差别；通道差异能解释"同一把 key 时灵时不灵"，不要据此改密钥或仓库。
   - **(b) key 绑在别的账号**：用 `ssh-keygen -l -f <key>.pub` 取指纹让用户在 Settings → SSH and GPG keys 逐字核对。
6. 结论必须给"两个独立症结"级别的拆解（key 未绑定 vs 仓库不存在），不要合成一个模糊的"权限问题"。

## 坑

- 不要重命名 origin 或把 GitHub 设成 origin——用户的 GitLab 工作流（部署脚本、其他会话）依赖 origin 指向不变。
- HTTPS 无凭据时不要反复重试或让用户贴 token：本机有 SSH key 就直接 `set-url` 换 SSH 协议。
- push 成功消息不等于远端落地：必须 fetch 回来比对 HEAD 再汇报。
- GitHub 镜像与 origin 分叉且镜像独有提交是**被撤出/越界的旧提交**（`git merge-base --is-ancestor <镜像独有sha> main` 为假）时：不要 merge（会引入冲突并把作废内容合进历史），也不要本地 `git merge` 后推；按项目 HANDOFF 预案重置镜像：`git push github origin/main:main --force-with-lease=main:<镜像当前sha>`，`--force-with-lease` 必须显式带期望 sha，防止覆盖别人刚推的内容。执行前确认该镜像独有的提交无他人工作依赖。
