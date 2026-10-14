---
name: git-mirror-add-remote
description: 给本地仓库新增 GitHub 远端镜像并推送（保留 origin，走 SSH）。
version: 1.0.0
author: hermes-agent
license: MIT
metadata:
  hermes:
    tags: [git, github, remote, push, mirror, ssh]
    related_skills: []
---

# 本地仓库新增 GitHub 远端镜像推送

## When to Use

用户要求把本机仓库与 GitHub 空仓库/仓库关联并 push，同时保留原 origin 不动（ros2_slice 等 gitlab 源仓库的常见操作）。

## 前置事实（开发机）

- 本机没有 `gh` CLI、没有 `~/.git-credentials`，HTTPS 推送直接失败（`could not read Username for 'https://github.com'`）。不要试 HTTPS 再回退——第一步就用 SSH 地址。
- `~/.ssh/config` 已配 `Host github.com`（IdentityFile `~/.ssh/id_ed25519_github` + IdentitiesOnly）。先 `ssh -T git@github.com` 确认返回 `Hi <用户名>!` 再动手。

## 步骤

1. `git remote -v && git status --short && git branch -vv`：确认 origin 指向、工作区干净、当前分支。
2. `git remote add github git@github.com:<owner>/<repo>.git`（SSH 形式）。
3. `git push -u github <当前分支>`：`-u` 让分支同时跟踪两个远端同名分支。
4. 验证：`git fetch github && git log --oneline -1 github/<分支>` 与本地 HEAD 一致，`git remote -v` 确认 origin 未被改动。

## 坑

- 推送前先 `git status`：脏工作区或用户未提交改动不影响 push（只推 commit），但要在汇报里说明哪些内容没有上 GitHub。
- 若用户给的 GitHub 地址是 https 形式，直接改写为 `git@github.com:` 形式执行，并说明原因（无 HTTPS 凭据、SSH 已认证）。
- 新增 remote 不改变 origin 的 fetch/push 行为；不要顺手 `set-url` 或改跟踪关系覆盖 origin。
- push 报 `Permission denied (publickey)` 时，`Server accepts key` 不等于 key 没绑对：先查 key 文件是否加密（`ssh-keygen -y -P '' -f <key>`）及当前会话有无 ssh-agent（`env -u SSH_AUTH_SOCK` 对照测试）——非图形会话常用无口令新钥根治。完整诊断层序见 git-github-mirror-remote 的「push 失败诊断」。
