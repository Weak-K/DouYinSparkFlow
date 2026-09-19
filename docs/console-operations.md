# 火花控制台运维手册

## 安全边界

控制台默认只监听 `127.0.0.1:8899`。没有可信域名与 HTTPS 时只可通过 SSH 隧道维护，不得放通公网端口。部署与回滚不得连接、停止或清理 BPS 的容器、镜像、网络、卷和端口。

Worker 只有一个实例、并发为 1；失败任务不会自动重试。启用 Worker 前必须确认系统时间误差不超过 5 秒、根分区空闲不少于 5 GiB，并记录现有业务容器健康状态。

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
- 兜底：该抖音号所属用户尚未填写邮箱（例如 `import-legacy` 导入的旧账号）时，改发给 `SPARK_ALERT_EMAIL_TO`，不会静默丢提醒。
- SMTP 未配置时跳过发送，且绝不影响任务执行与重试。

同一套 SMTP 还承担**其他任务失败的全局告警**（`alert_task_failure`）：任何非 `cookie_invalid` 的失败都会在**冷却窗口**（`SPARK_ALERT_COOLDOWN_MINUTES`，默认 60 分钟）内最多发一封给 `SPARK_ALERT_EMAIL_TO`，其余只计入「已抑制」，窗口过后下一封会带上被抑制的条数。`cookie_invalid` 不进这条路径——它已经按用户发给本人，不再重复。

在 `.env.console` 中填写发件邮箱（以 163 邮箱为例，授权码不是登录密码）：

```bash
SPARK_SMTP_HOST=smtp.163.com
SPARK_SMTP_PORT=465
SPARK_SMTP_SECURITY=ssl
SPARK_SMTP_USER=发件邮箱@163.com
SPARK_SMTP_PASSWORD=邮箱授权码
SPARK_SMTP_FROM=
SPARK_ALERT_EMAIL_TO=运维收件人@qq.com
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
