# 独立服务器 SSH 加固记录

本文记录 `103.236.79.17` 的 SSH 密钥登录配置与密码登录关闭过程，供后续维护、审计和回滚参考。

该主机是**独立服务器**，与火花控制台的 `wjdr`（`103.121.92.40`）不是同一台，二者密钥、权限域和运维流程相互独立，不要混用。

## 主机现状

| 项目 | 值 |
| --- | --- |
| 公网地址 | `103.236.79.17` |
| 主机名 | `C202611041976679` |
| 系统 | AlmaLinux 9.2 (Turquoise Kodkod) |
| SSH 服务端 | OpenSSH 8.7，监听 `0.0.0.0:22` |
| 时区 | `+08:00` |
| SELinux | Disabled |
| 登录用户 | `root` |

## 认证策略

变更后生效的策略（`sshd -T` 实测）：

```text
permitrootlogin yes
pubkeyauthentication yes
passwordauthentication no
kbdinteractiveauthentication no
```

服务端对外公布的认证方法为 `publickey,gssapi-keyex,gssapi-with-mic`，**`password` 已不在列表中**，即 SSH 层面无法再用密码登录。

`PermitRootLogin yes` 保持原样未调整。该值由 Anaconda 安装器生成的 `/etc/ssh/sshd_config.d/01-permitrootlogin.conf` 提供，关闭密码后 root 仅可凭密钥进入，风险面已收窄到密钥本身。

`sshd -T` 会额外打印 `main: sshd: ssh-rsa algorithm is disabled`，这是系统 crypto-policies 的正常提示，不是配置错误。

## 密钥与连接方式

专用密钥对，不复用 `~/.ssh/wjdr`，便于单独吊销而不影响控制台主机：

```text
私钥  ~/.ssh/id_ed25519_103.236.79.17   (ed25519, 0600, 无口令)
公钥  ~/.ssh/id_ed25519_103.236.79.17.pub
```

本机 `~/.ssh/config` 已登记别名，`srv103` 与裸 IP 均可直接使用：

```text
Host srv103 103.236.79.17
    HostName 103.236.79.17
    User root
    IdentityFile ~/.ssh/id_ed25519_103.236.79.17
    IdentitiesOnly yes
```

连接与自检：

```bash
ssh srv103 'sshd -T | grep -Ei "^(password|pubkey)authentication"'
```

## 实施步骤

以下流程可复用于同类加固，顺序不可调整——**必须先证明密钥可用，才能关闭密码**。

1. **确认凭据**。先用密码登录并核对系统与现有 `sshd` 配置，确认 `PubkeyAuthentication` 的实际取值。该机初始为 `no`，`/root/.ssh/authorized_keys` 是一个 0 字节空文件，说明此前从未配置过密钥。

2. **部署公钥并开启公钥认证**。

   ```bash
   mkdir -p /root/.ssh && chmod 700 /root/.ssh
   printf '%s\n' '<公钥内容>' > /root/.ssh/authorized_keys
   chmod 600 /root/.ssh/authorized_keys
   sed -i 's/^PubkeyAuthentication no/PubkeyAuthentication yes/' /etc/ssh/sshd_config
   sshd -t && systemctl reload sshd
   ```

   此步**保留** `PasswordAuthentication yes`，密码通道仍在，作为退路。

3. **验证密钥登录**。强制只用公钥，不依赖任何密码回落：

   ```bash
   ssh -i ~/.ssh/id_ed25519_103.236.79.17 -o IdentitiesOnly=yes \
       -o PreferredAuthentications=publickey -o BatchMode=yes root@103.236.79.17 'id'
   ```

   必须返回 `uid=0(root)` 才可继续。此步失败则停在原地，密码登录尚在，无锁定风险。

4. **挂安全网**。关闭密码前先创建一个定时回滚任务，超时未取消即自动恢复原配置：

   ```bash
   systemd-run --on-active=8min --unit=ssh-pwguard \
     --description="SSH password-auth rollback guard" \
     /bin/bash /root/.ssh_pwguard.sh
   ```

   `/root/.ssh_pwguard.sh` 的内容是把 `sshd_config` 备份覆盖回去并 `systemctl reload sshd`。用 `systemctl is-active ssh-pwguard.timer` 确认状态为 `active`。

5. **关闭密码登录**。

   ```bash
   sed -i 's/^PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
   sshd -t && systemctl reload sshd
   ```

   用 `reload` 而非 `restart`，已建立的会话不会中断。

6. **验证并撤网**。新开一个密钥会话确认可登录，再检查服务端认证方法列表：

   ```bash
   ssh -v -o PreferredAuthentications=password -o BatchMode=yes root@103.236.79.17 true 2>&1 \
     | grep 'Authentications that can continue'
   ```

   输出中不含 `password` 即为成功。此处刻意**不发送**密码，只观察服务端公布的方法列表，因此不会产生失败记录、不会触发 fail2ban。

   确认后撤除安全网：

   ```bash
   systemctl stop ssh-pwguard.timer
   systemctl reset-failed ssh-pwguard.timer ssh-pwguard.service
   rm -f /root/.ssh_pwguard.sh
   ```

   验证标准：`systemctl list-units --all 'ssh-pwguard*'` 计数为 0，`/root/.ssh_pwguard.log` 不存在（存在即说明回滚曾被触发）。

## 回滚

配置备份在服务器：`/etc/ssh/sshd_config.bak.20261104-210337`。

```bash
cp -f /etc/ssh/sshd_config.bak.20261104-210337 /etc/ssh/sshd_config
sshd -t && systemctl reload sshd
```

回滚会重新开放密码登录，仅在密钥通道失效且需要紧急进入时使用，事后应重新加固。

## 维护注意

- **私钥是唯一入口**。密码通道已封死，`~/.ssh/id_ed25519_103.236.79.17` 丢失、损坏或权限被改（必须是 `0600`）都会导致无法登录，只能通过服务商控制台的 VNC / 救援模式恢复。备份私钥时不要放进代码仓库。
- `authorized_keys` 的权限必须是 `700` 目录 + `600` 文件，属主 `root`。权限过宽时 sshd 会静默拒绝公钥。
- 该机 SELinux 为 Disabled，无需 `restorecon`。若日后重新启用 SELinux，需补 `restorecon -R /root/.ssh`。
- 本文**不记录任何密码**。初始密码曾有一组被服务器拒绝，说明凭据在交付前已变更或过期；凭据不应写入文档、聊天记录或版本库。
- 本次仅完成 SSH 认证加固。防火墙规则、fail2ban、系统更新、时间同步、主机名与时区均未调整，投入使用前应另行评估。

## 附录：本次操作踩坑

- **凭据误判**。首次交付的密码被拒 4 次。用 `expect` 伪终端和 `SSH_ASKPASS` 两种独立通道交叉验证，结论一致才判定为「凭据问题」而非「工具问题」，避免了继续重试触发 fail2ban 封禁出口 IP。连续失败超过 3 次即应停止尝试并要求核对凭据。
- **heredoc 变量提前展开**。把脚本经 `expect` 传递时，`<<'EOF'` 会让 `$VAR` 保持字面量，远程实际执行了空脚本且返回 0，静默失败。改为先把脚本 `base64` 编码写入文件、由 `expect` 读取后拼接，可彻底避开引号与嵌套 heredoc 问题。
- **服务名差异**。AlmaLinux / RHEL 系为 `sshd`，Debian / Ubuntu 系为 `ssh`，`systemctl reload` 时写错会直接报错。
- **`sshd_config.d` 优先级**。OpenSSH 采用「首次出现的值生效」，该机 `Include /etc/ssh/sshd_config.d/*.conf` 位于第 15 行（主配置顶部），因此 `.d/` 内的设置会覆盖文件后部的同名项。修改前应先确认目标值究竟来自哪一层，避免改了主配置却不生效。
