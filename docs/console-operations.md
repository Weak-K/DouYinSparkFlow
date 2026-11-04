# 火花控制台运维手册

## 安全边界

控制台默认只监听 `127.0.0.1:8899`。没有可信域名与 HTTPS 时只可通过 SSH 隧道维护，不得放通公网端口。部署与回滚不得连接、停止或清理 BPS 的容器、镜像、网络、卷和端口。

Worker 只有一个实例、并发为 1；发送前可恢复失败最多自动重试两次（1 分钟、5 分钟），消息提交后不自动重发。启用 Worker 前必须确认系统时间误差不超过 5 秒、根分区空闲不少于 5 GiB，并记录现有业务容器健康状态。

### 投递状态（2026-11-04）

执行器把每次发送的结果记为三种之一，不再靠 `stage` 和 `error_code` 反推能否重试：

| 状态 | 含义 | 可否重试 |
| --- | --- | --- |
| `not_sent` | 发送动作未发生，确定没发出去 | 技术上安全 |
| `uncertain` | 已按 Enter 但未拿到回执，可能已送达 | 禁止 |
| `confirmed` | 收到页面回执 | 不需要 |

边界是 **Enter 按下那一刻**。此后的一切异常——按键报错、页面崩溃、确认超时——都不足以证明消息没发出去，一律记为 `uncertain` 并禁止自动重试。

`retryable` 与 `safe_to_retry` 是两个不同的问题：前者是业务策略（`target_ambiguous` 技术上没发，但重试仍是歧义，故不重试），后者是技术安全性。改动发送逻辑时不要用其中一个去推导另一个。

Worker 会在日志里输出 `task_run delivery_state=... stage=... success=...`。`success=true` 且 `delivery_state=uncertain` 的组合表示「按已提交计成功，但没有送达证据」，排查重复消息时应优先看这一行。

### 同时到期任务与排队

- 在线 Worker 每次查询会登记所有按时到期的任务，再串行执行；有积压时立即领取下一条，空队列才等待 10 秒。
- 按时登记的任务可等待其他任务完成，不因排队超过 10 分钟而跳过；最大排队时限为计划时间后 1 小时。
- 首次发现已逾期 10 分钟的任务仍跳过。进程重启不继承内存队列，启动前错过且已超过 5 分钟的任务仍不补发。
- 排队许可绑定任务 ID 和本次计划时间；停用、删除或修改计划后旧许可失效。
- 重试按连续执行记录计数，排队超过 15 分钟不会重置两次重试额度。

好友快照滚动后会回到列表顶部并重新读取当前会话；点击按完整标题重新定位，不使用滚动前的行序号。选人整体限时 45 秒，搜索冷启动重搜后最多等待 8 秒；未找到目标可进行上述有限重试，打开错误会话仍中止发送。

### 2026-10-14：搜索完整标题与发送前保护

- 搜索仅检查 `.SearchPanelitembox` 内唯一、可见的 `.SearchPanelitemtitle` 完整标题，按既有规则归一化后与目标或已知别名比较。不使用全页 `get_by_text`，不把高亮子串当成完整姓名，也不放宽大小写。
- 点击前重读结果，按完整标题重新定位，避免位置 locator 被异步重排复用；“发消息”按钮仅在当前匹配条目内查找，不向上搜索整个面板。
- 多个完整标题与目标/别名相符时返回 `target_ambiguous`，不点击、不输入、不自动重试；宁可安全失败，不能取第一个结果猜收件人。
- 发送前最多等待 5 秒让会话标题切换，只接受唯一可见标题。标题不符仍为 `wrong_conversation_opened`；标题为空、不可读或不唯一为 `conversation_title_unavailable`，两者均在输入前中止。发送前临时失败仍沿用最多两次重试，提交消息后的不可安全重试边界不变。
- `conversation_not_opened` 表示选人或打开窗口阶段异常，不能据此断言已找到好友。路径/状态日志只记录固定枚举，不写姓名、身份 ID、Cookie 或消息正文。
- 验证范围包括真实 Chromium 的高亮子串、页面外同名文字、隐藏/重复结果、结果重排、按钮作用域、别名、标题延迟与缺失，以及执行器不输入/不发送保护。上线验收只打开目标会话，不补发历史失败任务。

### 2026-10-22：选人失败分类

- `target_not_found` 表示会话列表和精确搜索均未找到目标，不等同于 Cookie 失效。先用当前抖音账号在聊天搜索中确认任务填写的昵称或抖音号能显示该用户完整标题；若不能，核对名称是否变更及对方搜索/私信权限。若人工搜索可见而自动任务仍失败，记录任务 ID 与时间排查页面结构。只有明确的 `cookie_invalid` 才建议重新扫码。
- 首次打开聊天页时，目标选择先最多等待 15 秒让会话列表 hydration；列表未命中时，再最多等待 12 秒寻找所有可见、可用的搜索输入框。`target_search_unavailable` 表示这两步后仍没有找到可用搜索框，不能断言目标不存在；确认抖音聊天页已正常打开，若手动搜索可用则记录任务 ID 与时间排查页面结构。该失败可按 Worker 既有策略重试。

`spark-auth` 是独立的单实例扫码进程，只加入 `spark-private`，不发布宿主机端口、不挂载 Docker socket，并沿用只读根文件系统和 `no-new-privileges`。它与 `spark-web` 共享 `spark-data` 和两份只读密钥文件；不得把密钥内容写入 `.env.console`、命令行、聊天或日志。

## 首次安装

```bash
install -d -m 0750 /opt/douyin-spark-console/secrets
cp .env.console.example .env.console
docker compose --env-file .env.console -f compose.console.yml build spark-web spark-auth
docker volume create douyin-spark-console_spark-data
docker run --rm -u 0 -v douyin-spark-console_spark-data:/data douyin-spark-console-spark-web chown -R 10001:10001 /data
```

密钥必须在服务器本机生成，不复制到命令行、聊天或日志：

```bash
umask 077
head -c 32 /dev/urandom > secrets/cookie.key
head -c 32 /dev/urandom > secrets/session.key
chown 10001:10001 secrets/*.key
chmod 0400 secrets/*.key
```

只启动 Web 并验证回环地址：

```bash
docker compose --env-file .env.console -f compose.console.yml up -d --no-deps spark-web
curl --fail http://127.0.0.1:8899/health/ready
```

创建管理员时密码由隐藏提示读取，不出现在 shell 历史：

```bash
docker compose -f compose.console.yml run --rm spark-web python -m spark_console.cli create-admin admin
```

管理员可在网页创建普通用户；临时密码只显示一次，用户首次登录必须修改。

## 二维码与邀请功能升级

升级前记录当前控制台提交和容器状态，并执行 SQLite 备份。`backup-db` 输出卷内备份路径；必须保存该路径并确认命令成功后才能继续。备份文件、当前数据库和 `spark-data` 卷都不得删除。

```bash
git rev-parse HEAD
docker compose --env-file .env.console -f compose.console.yml ps
docker compose --env-file .env.console -f compose.console.yml run --rm spark-web python -m spark_console.cli backup-db
```

只构建 `spark-web` 与 `spark-auth`，然后只重建这两个服务。`--no-deps` 和显式服务名是部署边界：不要使用未列服务名的 `up`，不要启动或重建 `spark-worker`，也不要停用旧 timers。

```bash
docker compose --env-file .env.console -f compose.console.yml build spark-web spark-auth
docker compose --env-file .env.console -f compose.console.yml up -d --no-deps spark-web spark-auth
curl --fail https://wangze.oilu.cn/health/ready
docker compose --env-file .env.console -f compose.console.yml ps
```

验收时确认 `spark-auth` 为运行状态且 `PORTS` 为空，`spark-web` 仍只发布到回环地址；同时对照升级前记录，确认 BPS 容器、网络、卷、端口和旧 timers 都未改变。自动化检查不得生成或打印邀请码明文，也不得展示 Cookie、Token、存储状态、二维码历史或环境内容。

若验收失败，只停止 `spark-auth`，再切换到已审核的上一控制台提交，并只构建、重建 `spark-web`：

```bash
docker compose --env-file .env.console -f compose.console.yml stop spark-auth
git switch --detach <previous-console-commit>
docker compose --env-file .env.console -f compose.console.yml build spark-web
docker compose --env-file .env.console -f compose.console.yml up -d --no-deps spark-web
curl --fail https://wangze.oilu.cn/health/ready
docker compose --env-file .env.console -f compose.console.yml ps
```

此回滚不恢复数据库、不删除新增表、版本 2 账号、备份或 `spark-data` 卷，也不启动或重建 `spark-worker`。旧版 Web 不执行版本 2 账号，但数据会保留以便再次升级；原有 Cookie-only 账号和旧 timers 保持原状。严禁运行 `down -v`，严禁操作 BPS 资源。

## 域名与 HTTPS

生产环境保持 `SPARK_WEB_PUBLISH_IP=127.0.0.1` 和
`SPARK_SECURE_COOKIES=true`，由宿主机 Nginx 代理控制台。仓库中的
[`deploy/nginx/wangze.oilu.cn.conf`](../deploy/nginx/wangze.oilu.cn.conf)
是当前域名入口配置；部署后先运行 `nginx -t`，通过后再 reload：

```bash
sudo install -m 0644 deploy/nginx/wangze.oilu.cn.conf /etc/nginx/sites-available/wangze.oilu.cn
sudo ln -s /etc/nginx/sites-available/wangze.oilu.cn /etc/nginx/sites-enabled/wangze.oilu.cn
sudo nginx -t
sudo systemctl reload nginx
```

使用 Certbot 签发证书并启用 HTTP 到 HTTPS 跳转：

```bash
sudo certbot --nginx -d wangze.oilu.cn --redirect
sudo certbot renew --dry-run
```

完成后验证域名、证书和回环端口；公网不得再直接访问 8899：

```bash
curl --fail https://wangze.oilu.cn/health/ready
curl --fail http://127.0.0.1:8899/health/ready
```

## 旧账号导入

旧 JSON 只从服务器文件读取，命令输出只包含账号和任务数量：

```bash
docker compose -f compose.console.yml run --rm spark-web python -m spark_console.cli import-legacy /private/path/usersData.json --owner admin
```

导入前保留原文件；导入成功不代表可以切换。需逐账号执行不发送消息的登录及目标精确匹配验证。旧 systemd timers 在明确批准切换前保持原状。

## 启用与观察

确认系统时间、导入验证和维护窗口均通过后，才可以停用旧 timers 并启动 Worker：

```bash
docker compose -f compose.console.yml up -d spark-worker
docker compose -f compose.console.yml ps
```

不要在日志中输出 Cookie、密码、环境变量或聊天正文。只报告任务 ID、阶段、成功/失败及脱敏错误码。

## Cookie 失效提醒邮件

每个控制台账号在注册时必须填写**自己的通知邮箱**（`users.email`）。账号下绑定的抖音号各不相同，所以提醒邮件只发给该抖音号所属的用户本人，不存在全局群发。

- 触发点：Worker 执行任务时，抖音号返回 `cookie_invalid`，账号状态由「正常」变为「需要重新登录」。再次执行时不会重复发送，避免每天轰炸。
- 未填写邮箱：该抖音号所属用户没有通知邮箱时跳过发送，不转发给其他控制台账号。
- SMTP 未配置时跳过发送，且绝不影响任务执行与重试。

同一套 SMTP 还承担**任务失败告警**（`alert_task_failure`）：**最终失败**会在**每个任务主人的冷却窗口**（`SPARK_ALERT_COOLDOWN_MINUTES`，默认 60 分钟）内最多发一封给该任务主人的 `users.email`，邮件正文带控制台账号、抖音账号、目标和**任务 ID**。没有主人邮箱时跳过发送，不会发给 `SPARK_ALERT_EMAIL_TO`。两类失败不进这条路径：`cookie_invalid` 已经按用户发给本人，不再重复；`retry_scheduled_*` 只是「已安排 1 分钟 / 5 分钟自动重试」的**中间态**（Worker 自己还会再试，此时告警只会变成误报噪音），等重试跑完仍然失败时，最后那次会带**真实错误码**走到这里，该发的告警一封都不会少。

在 `.env.console` 中填写发件邮箱（以 163 邮箱为例，授权码不是登录密码）：

```bash
SPARK_SMTP_HOST=smtp.163.com
SPARK_SMTP_PORT=465
SPARK_SMTP_SECURITY=ssl
SPARK_SMTP_USER=发件邮箱@163.com
SPARK_SMTP_PASSWORD=邮箱授权码
SPARK_SMTP_FROM=
SPARK_ALERT_EMAIL_TO=独立测试邮件收件人@qq.com
SPARK_ALERT_COOLDOWN_MINUTES=60
```

`SPARK_SMTP_SECURITY` 可选 `ssl`（465）、`starttls`（587）、`plain`；`SPARK_SMTP_FROM` 留空时用 `SPARK_SMTP_USER`。冷却状态写在 `$SPARK_DATA_DIR/alert-state.json`（容器内 `/data`，随数据卷持久化）。

改完只重启 Worker 即可，不需要重建镜像：

```bash
docker compose --env-file .env.console -f compose.console.yml up -d --force-recreate spark-worker
docker compose --env-file .env.console -f compose.console.yml logs --tail=50 spark-worker
```

用户可在控制台「抖音账号」页随时改自己的通知邮箱；管理后台的用户列表会显示每人当前邮箱，未填写时显示「未填写」。排查发信问题时可在容器里直接跑一次连通性测试（会把测试信发给 `SPARK_ALERT_EMAIL_TO`）：

```bash
docker compose --env-file .env.console -f compose.console.yml run --rm spark-worker \
  python -c "import os; from spark_console.notify import MailSettings, send_mail; s = MailSettings.from_environ(os.environ); print('configured:', s.configured, 'recipients:', len(s.alert_recipients)); print('sent:', send_mail(s, s.alert_recipients, '【火花控制台】邮件通道测试', '连通性测试'))"
```

**在容器里跑测试时必须先清掉 SMTP 环境变量**，否则测试构造的失败记录会真的触发告警邮件：

```bash
docker compose --env-file .env.console -f compose.console.yml run --rm \
  -e SPARK_SMTP_USER= -e SPARK_SMTP_PASSWORD= -e SPARK_ALERT_EMAIL_TO= \
  spark-web python -m unittest discover -s tests/console -t tests/console -p "test_*.py"
```

## 备份与回滚

```bash
docker compose --env-file .env.console -f compose.console.yml run --rm spark-web python -m spark_console.cli backup-db
docker compose --env-file .env.console -f compose.console.yml stop spark-worker
```

回滚时先停止新 Worker，再重新启用原有 timers，并核对下一次触发时间。不要删除新数据库、旧配置、Docker 卷或备份。数据库恢复必须在 Web 和 Worker 均停止时进行，并先保留当前数据库副本。

撤销访问人员使用的临时 SSH 公钥是每次远程维护完成后的必做步骤。
