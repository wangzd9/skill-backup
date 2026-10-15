# 多环境代码版本对账（测试 / 生产 / 本地栈）

用「内容实测」代替「版本声明」：仓库里的版本字段和文档都会过期，只有逐字节比对运行中的文件才是事实。

## 三个不可信的版本声明

- `docs/deployment-snapshot.md` —— 脚本生成、只在部署时刷新，之后任何部署/回滚都会让它过期。
- 处理机 deploy 目录的 `.deployed-git-revision` —— 记录的是「写文件那一刻」的意图，后续同步、回滚、别人会话的部署都会覆盖它，且该文件本身不带内容校验。
- 主仓 `./scripts/local_stack.sh status` 的 `platform=<sha> stale=false` —— 读的是**主仓** `.local/runtime/started.json`，不反映正在运行的容器内容；本地栈被别的目录 `up` 过之后它会稳定地报旧值。

## 方法 A：生产实际内容 ↔ 候选提交逐字节对账

生产 deploy 目录是 rsync 同步、**无 `.git`**（`git rev-parse` 报「不是 git 仓库」），无法用 rev 直接问。用特征文件的 sha256 与候选提交比对：

```bash
# 生产侧（只读）
ssh -o BatchMode=yes processing-machine 'sha256sum /home/data-process/deploy/rosbag-catalog/<相对路径>'
# 本地侧对每个候选 sha
for sha in <候选1> <候选2>; do git show $sha:<相对路径> | sha256sum; done
```

- 选「特征文件」而不是全部：目标功能新增的模块、改过关键符号的模板/服务文件、schema 迁移目录。
- 迁移目录用文件名清单对账更快：`ls <deploy>/alembic/versions/ | sort` vs `git ls-tree --name-only -- alembic/versions/`（哪个迁移存在，直接说明生产 schema 停在哪条线）。
- 页面文案类功能再补一次实测：`ssh ... 'curl -s http://127.0.0.1:18000/<页面>'`，grep 新增/旧文案——比任何版本字段都直观。
- 关键判断：生产跑的版本可能是**任何远端都没有的提交**（别人本机分支直接部署）。这时「生产 = 最新 = 已发布」的假设全错，必须按未门禁上线如实汇报。

## 方法 B：本地栈其实是谁的代码

同一个 compose project 的容器可能来自**多个不同目录**——本地栈被不同 worktree 分别 `up` 过之后，labels 会保留各自的来源，`docker compose ps` 看不出来。

```bash
docker ps --format '{{.Names}}' | while read c; do
  printf '%s\t%s\n' "$c" "$(docker inspect $c --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
done
```

- 逐容器输出会暴露混合栈（例如 backend/scanner 来自一条功能分支的 worktree，frontend 来自另一个检查树，mysql 来自主仓）。
- **页面显示的 revision 只代表 backend 所在目录**（页面由 backend 渲染），frontend 容器来源不同不影响它；要判「本地栈有没有某功能」就对**容器内**文件算 hash：`docker exec <容器> sha256sum <路径>`，或直接看页面文案。
- 结论口径：本地栈不是任何单一版本的基线时，明确说「不能作为合入验证基线」，并给重建方案（`down` 后从目标 commit 的单目录 `up`）。

## 方法 C：分支/远端包含关系表

一次性列出所有候选提交的归属，避免口头推断「谁包含谁」：

```bash
for c in <候选sha...>; do
  printf '%-10s main:%s origin:%s github:%s\n' "$c" \
    "$(git merge-base --is-ancestor $c main && echo y || echo n)" \
    "$(git merge-base --is-ancestor $c origin/main && echo y || echo n)" \
    "$(git merge-base --is-ancestor $c github/main && echo y || echo n)"
done
git rev-list --left-right --count origin/main...main   # 左=远端独有 右=本地独有
git ls-remote origin refs/heads/main                   # 远端真实 tip，不信本地 remote-tracking
```

- `origin/main` 的 remote-tracking ref 可能陈旧，远端 tip 用 `git ls-remote` 实取。
- 从同一点分叉的两条线（都基于同一 base、互不包含）是最高危结论：谁先合谁都会丢另一边功能，两条都已「上线过」时要显式点出来。

## 交付格式（用户问「梳理环境与代码情况」时）

1. 一句加粗结论（例如「生产跑的是没走门禁的那条线，本地栈是三个目录混出来的」）。
2. 代码线逐条：sha / 分支 / 工作区路径 / 含什么 / 是否在本地 main、origin、github、生产。
3. 生产环境：`.deployed-git-revision`、容器健康、迁移 head、实测有/没有哪些功能。
4. 本地测试环境：容器来源目录、实际内容、页面实测 revision、陈旧记录。
5. 远端与仓库卫生：远端落后提交数、未提交改动、未跟踪遗留文件、多余 worktree/容器。
6. 风险按严重度排序（未经门禁上生产 → 两条线分叉 → 本地栈不代表任何版本 → 远端单点）。
7. 编号选项供用户拍板（合并补门禁 / 只隔离止血 / 只落盘梳理），并声明生产写入与合入各自等当次授权。

## 护栏

- 这类梳理默认**全程只读**：不合、不推、不部署、不动生产库；老改造（清理遗留脚本/容器）也只在选项里提出。
- 汇报「生产没部署某功能」时区分「从未部署」与「已回退」——两者处置不同，别用后者措辞。
