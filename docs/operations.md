# 使用说明与运维手册

第一部分写给受邀用户，第二部分写给运维者（目前就是你）。

## 一、使用说明（给受邀用户）

### 注册与登录
1. 安装 TestFlight 版 Gatherloom（知织），打开后点「使用邀请码注册」。
2. 填写用户名（3–32 位字母、数字、下划线、点或连字符）、密码（8–128 个字符）和收到的邀请码。邀请码只能用一次，默认 24 小时内有效。
3. 忘记密码时联系运维者重置；重置后所有设备都需要重新登录。

### 日常使用
- **记录**：在「记录」页写下想法，或在 Safari、相册中用系统分享把链接和截图发给 Gatherloom。没有网络时会先保存在手机上，恢复网络后自动上传。
- **项目**：为一件正在推进的事建一个项目，写下目标和「待解问题」，把相关记录加入项目。
- **AI 整理**：资料上传后，服务器会自动生成摘要和知识卡片，并建立索引。项目资料有变化时，大约 2 分钟后会更新「AI 总结」，并提出关联建议和决定草稿。
- **决定**：AI 只会提出草稿。只有你点「确认为正式决定」后，它才算你的决定。
- **问答**：在项目里提问，回答只依据这个项目的资料，并用 [n] 标出来源。点 [n] 可以查看原文，资料不足时会直接告诉你。
- **回顾**：每周一上午自动为上一周有变化的项目生成周回顾，也可以随时在项目页手动生成。

### 每日额度
每个账号每天的默认上限如下，按你账号的时区计算，零点重置：

| 项目 | 默认上限 |
|---|---|
| 新记录 | 200 条 |
| 提问 | 100 次 |
| 手动回顾 | 10 次 |
| AI 用量 | 约 ¥5 |

- 记录数达到上限后，新记录会保留在手机上，第二天自动上传。
- AI 用量达到上限后，当天不能再提问或手动回顾；后台整理会推迟到第二天继续。

### 数据去向与删除
- 你保存的内容存放在海外/港澳的服务器上。
- 资料正文、网页正文和截图文字会发送给阿里云百炼（通义千问、向量模型）和 DeepSeek，用于整理、检索、问答、总结和回顾。截图图片本身不会发送给模型服务，文字识别在手机上完成。
- 删除一条资料后，它会立即从列表、搜索和问答中消失，几分钟内从服务器上彻底清除，包括附件、卡片、引用片段、问答里的依据，以及后台任务的中间数据。
- 引用过它的回答会被隐藏。总结、回顾和正式决定保留文字，但会标出「依据已变化」。
- 每日备份最多保留 14 天。如果从备份恢复，已删除的资料会被再次删除。
- 把资料移出项目时，资料本身保留，但它不会再出现在这个项目的搜索和问答里。

## 二、运维手册

### 服务器与首次部署
前提：一台能运行 Docker Compose 的 Linux 服务器（ADR 0007：海外/港澳），一个指向它的域名，80 和 443 端口开放。

```sh
git clone <仓库> /srv/gatherloom && cd /srv/gatherloom
cp deploy/env.prod.example deploy/.env.prod
chmod 600 deploy/.env.prod
# 编辑 deploy/.env.prod：域名、数据库密码（openssl rand -base64 32）、模型密钥、额度
docker compose -f deploy/compose.prod.yaml --env-file deploy/.env.prod up -d --build
docker compose -f deploy/compose.prod.yaml --env-file deploy/.env.prod exec api python -m app.admin check-models
curl https://<域名>/healthz
```

结构：
- **对外入口**：只有 Caddy 发布 80 和 443 端口，证书自动签发和续期。
- **数据库**：PostgreSQL 在一个没有外网路由的内部网络里，不发布任何端口。
- **应用服务**：API、Worker 和 scheduler 使用同一个镜像，启动命令不同。每次启动前，`migrate` 会先执行数据库迁移。
- **数据卷**：`postgres-data`（数据库）、`storage`（附件）、`state`（删除日志）、`caddy-data`（证书）。

### 服务器上已有反向代理
80 和 443 已被 nginx 等占用时，由它终止 TLS，Caddy 只在本机端口提供 HTTP。在 `deploy/.env.prod` 中设置：

```sh
GATHERLOOM_DOMAIN=:80
HTTP_PORT=127.0.0.1:8100
HTTPS_PORT=127.0.0.1:8143
```

外层代理转发到 `http://127.0.0.1:8100`，请求体上限不低于 12 MB，并把 `X-Forwarded-For` 覆盖为真实客户端地址（nginx：`proxy_set_header X-Forwarded-For $remote_addr;`）。登录限流按这个地址计算。Caddy 只信任来自私有网段的 `X-Forwarded-For`。

下文用 `dc` 代指 `docker compose -f deploy/compose.prod.yaml --env-file deploy/.env.prod`。

### 更新版本
```sh
scripts/deploy.sh     # 快进到 origin/main，重建并重启，等待 api 健康
```
`migrate` 会先跑完迁移，再启动新版本。API 在 2 分钟内没有恢复健康时，脚本打印日志并以非零状态退出。

### 自动部署
合并到 `main` 且改动涉及 `server/`、`deploy/`、`scripts/` 时，`Server checks` 工作流在检查通过后通过 SSH 运行服务器上的 `scripts/deploy.sh`。也可以在 Actions 页面手动运行该工作流（选择 `main`）。

一次性配置：
1. 在服务器上生成专用密钥：`ssh-keygen -t ed25519 -N '' -C gatherloom-github-actions -f ~/.ssh/gatherloom_github_actions_ed25519`。
2. 把公钥加入 `~/.ssh/authorized_keys`，并限制为只能运行部署脚本：
   `command="/srv/gatherloom/scripts/deploy.sh",restrict ssh-ed25519 AAAA… gatherloom-github-actions`
3. 在 GitHub 仓库建立 `production` 环境（部署分支限定 `main`），添加 secrets：`DEPLOY_HOST`、`DEPLOY_USER`、`DEPLOY_SSH_KEY`（私钥）、`DEPLOY_KNOWN_HOSTS`（`ssh-keyscan -t ed25519 <主机>` 的输出，核对指纹后填入），端口不是 22 时加 `DEPLOY_PORT`。

服务器上的检出目录需要能 `git fetch origin main`，并保持在 `main` 分支、没有本地改动。

### 邀请用户
```sh
dc exec api python -m app.admin invite --hours 48
```
每次生成一个一次性邀请码，有效时长为 1–168 小时。请通过私密渠道发给对方，并附上第一部分的使用说明。

### 重置密码
```sh
dc exec api python -m app.admin reset-password <用户名>
```
按提示输入两次新密码。重置后，这个账号的所有登录会话都会失效。

### 调整额度
修改 `deploy/.env.prod` 里的 `GATHERLOOM_DAILY_*` 后，执行 `dc up -d` 重启服务。设为 0 表示不限。

花费按 `runs` 表里的估算成本计算：人民币原样计入，美元按 `GATHERLOOM_USD_CNY` 折算。查看各账号当天的花费：

```sh
dc exec postgres psql -U gatherloom -d gatherloom -c \
  "select u.username, r.currency, sum(r.cost_estimate) from runs r join users u on u.id = r.owner_id
   where r.created_at > now() - interval '1 day' group by 1, 2 order by 3 desc"
```

### 删除与清理
- 用户删除资料后，scheduler 每 5 分钟清理一次，只处理删除时间超过 2 分钟的资料，确保 Worker 的租约已经失效。
- 想立刻清理时运行 `dc exec api python -m app.admin purge-deleted`。
- 删除清单记在 `deletions` 表中，同时追加写入 `state` 卷里的 `deletions.jsonl`，供从备份恢复后重放。
- 上传后 24 小时内没有关联到记录的附件，用 `dc exec api python -m app.admin cleanup-attachments` 清理。可以把它和备份一起放进每日 cron。

### 备份
`scripts/backup.sh` 备份以下内容：
- 数据库（`pg_dump` 自定义格式）
- 附件
- 删除日志
- `deploy/.env.prod`

每次备份写入 `BACKUP_DIR/<时间戳>/`，附带校验和，超过 `BACKUP_KEEP_DAYS`（默认 14）天的会被删除。设置了 `BACKUP_REMOTE` 时，还会用 rsync 同步一份到异地。

```sh
# root 的 crontab
15 3 * * * cd /srv/gatherloom && scripts/backup.sh >> /var/log/gatherloom-backup.log 2>&1
30 3 * * * cd /srv/gatherloom && docker compose -f deploy/compose.prod.yaml --env-file deploy/.env.prod exec -T api python -m app.admin cleanup-attachments
```

备份里有模型密钥和数据库密码，异地目的地必须可信，或者落盘加密。

### 从备份恢复
```sh
scripts/restore.sh /var/backups/gatherloom/<时间戳> --yes
```

脚本会依次执行：
1. 核对校验和，停掉对外服务。
2. 重建数据库并导入备份，用备份替换附件。
3. 把服务器上现有的删除日志和备份里的合并。
4. 执行迁移，再执行 `replay-deletions`：备份之后被删除的资料会被再次删除并清理。
5. 最后才重新启动服务。

如果整台服务器都丢失了，先按「首次部署」装好新服务器，再把备份里的 `env.prod` 放到 `deploy/.env.prod`，然后运行上面的恢复命令。这种情况下，删除日志只能恢复到最近一次备份为止。

恢复演练记录：
- 2026-11-11 在本地生产栈演练过一次：备份后删除的资料，在恢复后被再次删除，正文已清空。
- 上线后需要在正式服务器上再演练一次。

### 日志与排查
- `dc logs -f api worker scheduler`：日志是 JSON 格式，每个任务都记录 `job_id`、`stage`、`status` 和 `latency_ms`。
- 有资料一直没整理完时，先看 Worker 日志有没有 `daily model budget reached`（当天预算已用完，任务会推迟到第二天），再看 `dc exec api python -m app.admin check-models` 能不能正常调用模型。
- 登录被限流（HTTP 429）：同一 IP 每分钟最多尝试 10 次，下一分钟自动恢复。
