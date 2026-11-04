# 独立服务器部署记录（103.236.79.17）

本文记录火花控制台在 `103.236.79.17` 的完整部署过程与日常运维方式。该部署是**全新环境**，与已下线的 `wjdr` 无数据继承关系。

## 环境概览

| 项目 | 值 |
| --- | --- |
| 地址 | `103.236.79.17`（SSH 别名 `srv103`） |
| 系统 | AlmaLinux 9.2 |
| 规格 | 8 核 / 7.8 GiB / 50 GB |
| Docker | 29.9.3（containerd 镜像存储）+ compose 5.7.0 |
| 代码目录 | `/opt/douyin-spark-console` |
| 访问入口 | `http://103.236.79.17:8899` |

SSH 密钥与加固过程见 [server-ssh-hardening.md](server-ssh-hardening.md)。

## 与 wjdr 部署的关键差异

| 维度 | wjdr（已到期下线） | 本机 |
| --- | --- | --- |
| 反向代理 | 宿主机 Nginx + 域名 HTTPS | 无，Docker 直接发布 8899 |
| `SPARK_WEB_PUBLISH_IP` | `127.0.0.1` | `0.0.0.0` |
| `SPARK_SECURE_COOKIES` | `true` | `false` |
| 数据 | 长期运行的生产库 | 空库，重新初始化 |
| 防火墙 | 有 | firewalld 未启用，端口直接暴露 |

`wjdr` 于 2026-11-04 到期，应用层完全不可达（TCP 可连但 SSH banner 超时、8899 超时），`spark.db` 与 `cookie.key`/`session.key` 未能导出。因此本机无法沿用旧数据：**所有抖音账号需重新扫码绑定，任务需重建**。

## 本机特有的网络适配

这台机器是境内线路，且网络质量较差（百度的响应时间都要 8 秒），部署时有三处必须调整：

- **Docker Hub 完全不通**（`registry-1.docker.io` 超时），`mcr.microsoft.com` 能握手但**拉不动数据**（连接建立后 `Recv-Q` 恒为 0，3 分钟 0 字节落地）。
- Docker 只能从阿里云源安装，官方 `download.docker.com` 不可达。
- 基础镜像必须经 `m.daocloud.io` 代理拉取，约 0.7–1.6 MB/s，3.63 GB 耗时约 20 分钟。

实际做法：

```bash
# 1) 安装 Docker（阿里云源）
curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo \
  -o /etc/yum.repos.d/docker-ce.repo
dnf install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

# 2) 经代理拉基础镜像，再打回 Dockerfile 引用的原始 tag
docker pull m.daocloud.io/mcr.microsoft.com/playwright/python:v1.56.0-noble
docker tag  m.daocloud.io/mcr.microsoft.com/playwright/python:v1.56.0-noble \
            mcr.microsoft.com/playwright/python:v1.56.0-noble

# 3) 构建（pip 走清华源）
docker compose --env-file .env.console -f compose.console.yml build
```

打 tag 这一步让 `Dockerfile.console` 保持原样，不必为了网络环境改动项目文件。

`docker.1ms.run` 与 `dockerproxy.net` **不支持** `mcr.microsoft.com` 前缀（返回 `not found`），只有 `m.daocloud.io` 可用。

## 初始化

```bash
cd /opt/douyin-spark-console
cp .env.console.example .env.console
sed -i 's/^SPARK_SECURE_COOKIES=.*/SPARK_SECURE_COOKIES=false/' .env.console
sed -i 's/^SPARK_WEB_PUBLISH_IP=.*/SPARK_WEB_PUBLISH_IP=0.0.0.0/' .env.console
sed -i 's|^SPARK_BUILD_PIP_INDEX_URL=.*|SPARK_BUILD_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple|' .env.console

install -d -m 0750 secrets
umask 077
head -c 32 /dev/urandom > secrets/cookie.key
head -c 32 /dev/urandom > secrets/session.key
chown 10001:10001 secrets/*.key && chmod 0400 secrets/*.key

docker volume create douyin-spark-console_spark-data
docker run --rm -u 0 -v douyin-spark-console_spark-data:/data \
  douyin-spark-console-spark-web chown -R 10001:10001 /data
```

管理员密码由 `getpass` 读取，可经管道非交互传入（会打印 `GetPassWarning`，属正常）：

```bash
printf '%s\n%s\n' "$PW" "$PW" | docker compose --env-file .env.console \
  -f compose.console.yml run -T --rm spark-web python -m spark_console.cli create-admin admin
```

初始密码写在 `secrets/admin-password.txt`（0600）。**首次登录强制跳转 `/change-password`**，改完才能进控制台。

## 日常运维

所有命令都要带 `--env-file .env.console -f compose.console.yml`，并在 `/opt/douyin-spark-console` 目录下执行。

```bash
# 状态
docker compose --env-file .env.console -f compose.console.yml ps

# 改代码后重建（镜像靠 COPY . /app，restart 无效）
docker compose --env-file .env.console -f compose.console.yml build
docker compose --env-file .env.console -f compose.console.yml up -d

# 日志（不在 stdout，在容器内 /tmp/spark-logs/app.log）
docker exec douyin-spark-console-spark-worker-1 tail -50 /tmp/spark-logs/app.log

# 健康检查
curl http://127.0.0.1:8899/health/ready
```

三个容器均为 `restart: unless-stopped`，Docker 已 `enable`，重启机器会自动拉起。Worker 空闲时不写日志（只有任务执行和 Cookie 通知会记录），日志为空属正常。

## 已知风险

- **明文 HTTP 暴露公网**。`SPARK_SECURE_COOKIES=false` 是为了让 IP 直连能登录，代价是登录密码和 session cookie 全程明文传输。任何在公网链路上的人都能截获。改善方式：加域名 + HTTPS 后把 `SECURE_COOKIES` 改回 `true`，或改用 SSH 隧道访问。
- **登录无失败次数限制**。项目的 `failed_login_count` 只累加，`locked_until` 未被使用，8899 直接对公网开放时存在暴力破解风险。
- **firewalld 未启用**。除 8899 外，SSH 22 也直接暴露；建议后续启用 firewalld 或配置云安全组，只放行必要端口。
- **无数据备份**。`spark.db` 在 `douyin-spark-console_spark-data` 卷内，尚未配置定期备份。旧环境正是因为到期未迁移而丢了全部数据，建议尽快补上 `backup-db` 的定时任务。
