---
name: dev-machine-ssh-connections
description: "Use when 开发机新增/修复 SSH 连接、多密钥认证失败或整理连接别名清单。"
version: 1.0.0
author: hermes-agent
metadata:
  hermes:
    tags: [ssh, dev-machine, connections]
---

# 开发机 SSH 连接：新增与修复

## When to Use

用户要连一台新机器（含报错 `Too many authentication failures`），或问"本机都能连哪些机器/别名是什么"。既有连接事实以 `~/.ssh/config` 为唯一来源（语义别名 + 专用密钥 + `IdentitiesOnly yes`）；清单类回答直接读 config 生成对照表，不凭记忆。

## 报错 Too many authentication failures 的修复流程

**机制**：本机 `~/.ssh/` 多把密钥（含默认 id_rsa 等）会被逐个发给服务器，未认证尝试超过服务端 `MaxAuthTries` 即断连；agent 为空也不豁免（客户端仍按默认顺序试文件）。修复=专用密钥+config 限定，不删其他密钥（可能属于别的会话/服务）。

1. `ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_<语义名> -N '' -C <用途>`。
2. `~/.ssh/config` 追加 Host 块：语义别名、HostName、User、`IdentityFile`、`IdentitiesOnly yes`、`BatchMode yes`（BatchMode 保证之后绝不回退密码/交互）。**先 read_file 现有 config 再用 patch 追加**，不整文件覆盖。
3. 一次性密码注入公钥（本机无 sshpass/expect 时的标准法）：写临时 askpass 脚本（`#!/bin/bash` + `echo '<密码>'`）到 scratch 目录 chmod 700，然后
   `SSH_ASKPASS=<脚本> SSH_ASKPASS_REQUIRE=force DISPLAY=:0 setsid ssh -o StrictHostKeyChecking=accept-new -i <新私钥> user@host 'cat >> ~/.ssh/authorized_keys' < <新公钥>`
4. 验证 `ssh <别名> 'hostname; whoami'`（BatchMode 下能回即免密成立）。
5. **删临时 askpass 脚本**；密码不得写入仓库、config 或任何持久文件。

## Pitfalls

- 远端 `sh -c` 单行里变量赋值后接命令合法，**变量名以 `.` 开头不合法**（bash 报 No such file or directory）；且 `$(cat /dev/stdin)` 在 stdin 已被消费后展开为空——多行远端脚本用 stdin heredoc（`ssh host <<'EOF' ... EOF`），别在 `-c` 串里拼复杂脚本。
- 新机器别名按用途语义命名（推理服务器 `llm-server`、数据处理 A100 `a100-wzd`、处理机 `processing-machine`）；**远端 `hostname` 可能与既有别名撞名（曾有 hostname 就叫 `a100` 的推理服务器）**——身份以 SSH 别名+IP 为准，别拿远端 hostname 反查机器。
- 回答"连接清单"时给：别名/地址/用户/端口/用途/密钥 对照表 + 统一用法 `ssh <别名>`（别用裸 IP，裸 IP 正是触发该报错的方式）。
- 弱密码公网机：完成公钥注入后提醒用户考虑禁用密码登录（`PasswordAuthentication no`）。
- **git 仓库 remote URL 指向裸 IP/主机名而专用密钥只经 config 别名才生效时，`git fetch/pull` 报 `Permission denied (publickey,password)`**——git 走 `git@<host>` 不套用 config 里的 `Host <别名>` 块（含 `IdentityFile`/`IdentitiesOnly yes`），等于又回到裸 IP 免密失败。诊断：`git remote -v` 看 origin 是否裸 IP，`ssh -o BatchMode=yes <别名>` 能回则密钥+别名组合本身是好的。临时验证（或暂不改 URL 时）用一次性覆盖：`GIT_SSH_COMMAND="ssh -i ~/.ssh/<专用密钥> -o IdentitiesOnly=yes -o BatchMode=yes" git fetch origin`。修复：`git remote set-url origin git@<别名>:<group>/<repo>.git` 改用别名，再 `git branch --set-upstream-to=origin/<branch>`（顺带修正可能指向旧镜像 remote 的上游，避免误显"领先/落后"），最后 `git fetch origin` 验证。同一仓库另配了 GitHub 镜像 remote 时，`git pull` 默认仍走 branch 的上游，改完 URL 后上游也要一起核对。
