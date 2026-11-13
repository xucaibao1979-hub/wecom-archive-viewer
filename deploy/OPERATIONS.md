# 会话存档查看器运维手册（Ubuntu 22.04）

首次安装见 [UBUNTU-22.04.md](UBUNTU-22.04.md)。本手册适用于已将代码、配置与用户库分开存放的部署。

## 目录与备份范围

| 位置 | 内容 | 更新程序时的处理 |
| --- | --- | --- |
| `/opt/archive-viewer` | 应用代码、部署脚本、虚拟环境 | 更新代码；`.venv` 可重建 |
| `/opt/archive-data/.env` | 数据库连接、密钥等配置 | 保留，不随代码包覆盖 |
| `/opt/archive-data/viewer_users.db` | 账号、权限、免登授权和审计 | 保留，使用 SQLite 在线备份 |
| `/opt/archive-data/cache` | 音频转码缓存 | 可重建，不纳入备份 |
| `/opt/archive-backups` | 每日备份包 | 权限 700，另做异机备份 |

存档消息保存在**外部 PostgreSQL**，媒体文件位于 `.env` 的 `VIEWER_MEDIA_ROOT`。它们不包含在查看器的本地备份包中，须由各自的数据源或存储服务独立备份。Nginx、证书和安装在 `/etc/systemd/system` 的服务文件也应由服务器配置备份覆盖。

## 日常检查

```bash
sudo systemctl status archive-viewer --no-pager
sudo journalctl -u archive-viewer -n 100 --no-pager
curl -I http://127.0.0.1:8765/
sudo nginx -t
sudo -u archive test -r /opt/archive-data/.env && echo '配置可读取'
```

`127.0.0.1:8765` 是应用的本机监听地址；用户应从 HTTPS 域名经 Nginx 访问。若本机端口拒绝连接，先查 `journalctl`；若本机正常而网页 502，检查 Nginx 转发和服务状态。密码验证成功却回到登录页时，检查是否通过 HTTPS 域名访问，以及 `.env` 的 `VIEWER_COOKIE_SECURE=true`。

管理员密码异常时，使用终端交互式工具，不修改或删除用户库：

```bash
sudo -u archive /opt/archive-viewer/.venv/bin/python /opt/archive-viewer/deploy/check_login.py --username admin
sudo -u archive /opt/archive-viewer/.venv/bin/python /opt/archive-viewer/deploy/reset_admin.py --username admin
```

第二条命令仅在需要重设时运行。初次建号后修改 `.env` 的 `VIEWER_ADMIN_PASSWORD` 不会更改现有密码。

## 代码升级

升级前先确认已有成功备份。部署 ZIP 应只包含代码和 `.env.example`，不得包含服务器的 `.env`、用户库或媒体文件。

如果服务器上还没有新版 `backup_project.py`，先从新包单独取出该脚本放到 `/opt/archive-viewer/deploy/` 并设置属主为 `archive`，再执行下列备份命令；不要因为脚本不存在而跳过备份。

```bash
sudo -u archive /opt/archive-viewer/.venv/bin/python /opt/archive-viewer/deploy/backup_project.py
sudo systemctl stop archive-viewer
sudo unzip -o /tmp/archive-viewer-YYYYMMDD.zip -d /opt/archive-viewer
sudo chown -R archive:archive /opt/archive-viewer
sudo -u archive -H bash -lc 'cd /opt/archive-viewer && ~/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt gunicorn'
sudo systemctl start archive-viewer
sudo systemctl status archive-viewer --no-pager
```

若当天已有备份，备份脚本会拒绝覆盖；核对该包可用后再升级。若服务单元发生变化，复制新版 `deploy/archive-viewer.service` 至 `/etc/systemd/system/` 并执行 `sudo systemctl daemon-reload`。升级后检查登录、群聊、媒体预览及免登入口。已有部署首次迁移到 `/opt/archive-data` 时，请先按首次安装文档的“旧部署迁移”小节操作。

## 每日备份与保留 30 天

备份脚本 [`backup_project.py`](backup_project.py) 将代码和本地持久化数据归入同一个 `/opt/archive-backups/archive-viewer-YYYYMMDD.tar.gz`。同一天不覆盖已有包；成功生成并校验后，删除超过最近 30 个自然日的**同名格式**备份。包内代码排除 `.git`、`.venv`、缓存及本地密钥；本地数据排除可重建的 `cache/`。用户库通过 SQLite 在线备份接口复制。

先手工运行并核查：

```bash
sudo install -d -o archive -g archive -m 700 /opt/archive-backups
sudo -u archive /opt/archive-viewer/.venv/bin/python /opt/archive-viewer/deploy/backup_project.py
sudo tar -tzf /opt/archive-backups/archive-viewer-$(date +%Y%m%d).tar.gz | head
sudo ls -lh /opt/archive-backups/archive-viewer-$(date +%Y%m%d).tar.gz
```

执行 `sudo crontab -u archive -e`，加入每天凌晨 02:30 的任务：

```cron
30 2 * * * /usr/bin/flock -n /opt/archive-backups/.backup.lock /opt/archive-viewer/.venv/bin/python /opt/archive-viewer/deploy/backup_project.py
```

检查 `sudo crontab -u archive -l`。仓库还保留旧的 `archive-viewer-backup.timer`，它只备份 `.env` 和用户库；若曾启用，执行 `sudo systemctl disable --now archive-viewer-backup.timer`，避免同时运行两套计划任务。备份目录与服务器在同一磁盘时，磁盘故障仍会同时丢失原件与备份，建议定期复制到另一台主机。

## 恢复

恢复会回退备份之后创建的账号、权限和配置。先确认备份日期、保留当前目录以便回退，并确保外部 PostgreSQL 与媒体存储可用。以下假设恢复包为 `archive-viewer-YYYYMMDD.tar.gz`，把日期替换为实际值：

```bash
BACKUP=/opt/archive-backups/archive-viewer-YYYYMMDD.tar.gz
sudo tar -tzf "$BACKUP" | head
sudo systemctl stop archive-viewer
STAMP=$(date +%Y%m%d-%H%M%S)
sudo mv /opt/archive-viewer "/opt/archive-viewer.before-restore-$STAMP"
sudo mv /opt/archive-data "/opt/archive-data.before-restore-$STAMP"
sudo tar -xzf "$BACKUP" -C /opt
sudo chown -R archive:archive /opt/archive-viewer /opt/archive-data
sudo chmod 700 /opt/archive-data
sudo chmod 600 /opt/archive-data/.env /opt/archive-data/viewer_users.db
sudo -u archive -H bash -lc 'cd /opt/archive-viewer && ~/.local/bin/uv venv --python 3.12 .venv && ~/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt gunicorn'
sudo -u archive /opt/archive-viewer/.venv/bin/python -c 'import sqlite3; db=sqlite3.connect("/opt/archive-data/viewer_users.db"); print(db.execute("PRAGMA quick_check").fetchone()[0]); db.close()'
sudo systemctl start archive-viewer
sudo systemctl status archive-viewer --no-pager
```

确认 `quick_check` 输出 `ok`，再通过 HTTPS 页面验证登录、权限、群聊和媒体。若恢复失败，保持服务停止，将新解压的两个目录移开，再把同一 `STAMP` 对应的 `.before-restore-*` 目录移回原位置并启动服务。确认恢复稳定后，再按保留策略处理回退副本。

## 旧部署迁移

只在旧版将 `.env` 或 `viewer_users.db` 放在代码目录或 `/var/lib/archive-viewer` 时执行一次。先停止服务，运行 `deploy/migrate_user_db.py`；该脚本拒绝覆盖目标数据。确认 `/opt/archive-data/.env` 的 `VIEWER_USER_DB=/opt/archive-data/viewer_users.db` 后再启动服务。不要因为“找不到 admin”就删除或重建用户库；应先确认程序当前连接的库路径。
