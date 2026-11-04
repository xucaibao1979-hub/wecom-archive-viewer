# Ubuntu 22.04 全新部署

此包不含数据库内容、用户账号、免登密钥和本地媒体文件。将以下示例域名 `archive.example.com` 替换为实际域名。域名应先解析到服务器，服务器需能访问存档 PostgreSQL。只开放 80/443；8765 仅监听本机。

## 1. 上传并解压

在 Windows PowerShell 中：

```powershell
scp .\archive-viewer-ubuntu22.04.zip user@服务器IP:/tmp/
```

登录 Ubuntu：

```bash
sudo apt update
sudo apt install -y curl unzip nginx snapd
sudo useradd --system --create-home --shell /bin/bash archive
sudo mkdir -p /opt/archive-viewer
sudo install -d -o archive -g archive -m 700 /opt/archive-data
sudo unzip /tmp/archive-viewer-ubuntu22.04.zip -d /opt/archive-viewer
sudo chown -R archive:archive /opt/archive-viewer
```

## 2. 安装 Python 与依赖

```bash
sudo -u archive -H bash -lc 'curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u archive -H bash -lc 'cd /opt/archive-viewer && ~/.local/bin/uv python install 3.12 && ~/.local/bin/uv venv --python 3.12 .venv && ~/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt gunicorn'
```

## 3. 填写配置并初始化管理员

```bash
sudo cp /opt/archive-viewer/.env.example /opt/archive-data/.env
sudo nano /opt/archive-data/.env
```

填写 PostgreSQL 的 `WECOM_DB_*`、服务器媒体目录 `VIEWER_MEDIA_ROOT`、管理员账号和密码。`VIEWER_PUBLIC_ORIGIN` 填实际 HTTPS 域名；`VIEWER_COOKIE_SECURE=true`。使用 `openssl rand -hex 32` 生成 `VIEWER_SECRET_KEY`。服务器上的数据库账号应只有存档读取权限。媒体文件若在另一台机器，需要挂载到本机并给 `archive` 用户读取权限。

```bash
sudo chown archive:archive /opt/archive-data/.env
sudo chmod 600 /opt/archive-data/.env
sudo -u archive bash -lc 'cd /opt/archive-viewer && .venv/bin/python -c "import app; app.bootstrap_admin()"'
```

保持 `.env.example` 中的 `VIEWER_USER_DB=/opt/archive-data/viewer_users.db`。这会在独立目录创建全新的用户库和管理员，不使用本地数据。初始化后修改 `.env` 的初始管理员密码不会修改已有账号；后续密码在网页设置中更改。

若首次管理员未创建或密码不对，执行 `sudo -u archive /opt/archive-viewer/.venv/bin/python /opt/archive-viewer/deploy/reset_admin.py --username admin`。工具会创建不存在的管理员，或重设已有管理员的密码并启用账号。按提示在终端输入新密码（输入时不会显示字符），无需删除用户库。密码不设长度限制，但不能为空。若想使用其他管理员用户名，替换 `--username` 参数。

登录失败时可先运行 `sudo -u archive /opt/archive-viewer/.venv/bin/python /opt/archive-viewer/deploy/check_login.py --username admin`。它会在终端隐式读取密码，只输出账号是否存在/启用、密码是否匹配及 HTTPS/Cookie 配置，不输出密码或摘要。若 `password_matches: True` 但网页仍回到登录页，请确认浏览器使用 HTTPS 域名，且 `VIEWER_COOKIE_SECURE=true`；HTTP 局域网地址无法保存 Secure Cookie。

## 4. 启动服务

```bash
sudo cp /opt/archive-viewer/deploy/archive-viewer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now archive-viewer
sudo systemctl status archive-viewer
curl -I http://127.0.0.1:8765/
```

## 5. Nginx 与 HTTPS

先把 `deploy/archive-viewer.nginx.conf` 中的 `archive.example.com` 改成实际域名，再安装：

```bash
sudo cp /opt/archive-viewer/deploy/archive-viewer.nginx.conf /etc/nginx/conf.d/archive-viewer.conf
sudo nano /etc/nginx/conf.d/archive-viewer.conf
sudo nginx -t
sudo systemctl reload nginx
sudo snap install core
sudo snap refresh core
sudo snap install --classic certbot
sudo ln -s /snap/bin/certbot /usr/bin/certbot
sudo certbot --nginx -d archive.example.com
```

Certbot 应配置 HTTP 自动跳转 HTTPS。Nginx 日志格式只记录路径，不记录一次性票据查询参数。其他代理或 WAF 的访问日志也需避免记录 `/sso/login?ticket=...`。不要把长期免登密钥或完整 `loginUrl` 写入日志。

## 6. 验证与日常维护

访问 `https://实际域名`，使用 `.env` 中首次管理员账号登录。检查客户列表、群聊会话和媒体文件；在“设置 → 用户与权限”创建用户、分配菜单权限并生成免登密钥。调用方后端用 `X-SSO-Key` 请求 `POST /api/sso/ticket`，仅将返回的 `loginUrl` 交给浏览器。票据用过后再次打开应拒绝。

浏览器必须访问 HTTPS 域名。若用 `http://192.168.1.10:8765` 等 HTTP 地址访问，`VIEWER_COOKIE_SECURE=true` 会阻止浏览器保存登录 Cookie，出现密码验证后又回到登录页的现象。保留 Gunicorn 监听 `127.0.0.1:8765`，由 Nginx 对外提供 HTTPS。

```bash
sudo journalctl -u archive-viewer -n 100 --no-pager
sudo systemctl restart archive-viewer
sudo systemctl status archive-viewer
```

升级代码前备份服务器的 `.env` 和用户库。用户库保存用户、菜单权限、免登授权摘要、票据和审计日志，应纳入服务器备份。多主机部署不能各自持有独立的本地用户库。

可安装每日 03:00 的本地备份任务（备份目录应另行纳入服务器或异机备份）：

```bash
sudo install -d -o archive -g archive -m 700 /opt/archive-backups
sudo cp /opt/archive-viewer/deploy/archive-viewer-backup.service /etc/systemd/system/
sudo cp /opt/archive-viewer/deploy/archive-viewer-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now archive-viewer-backup.timer
sudo systemctl start archive-viewer-backup.service
sudo systemctl status archive-viewer-backup.service --no-pager
```

备份工具使用 SQLite 在线备份接口复制 `viewer_users.db`，并复制 `.env`；两者与 SHA-256 清单保存在 `/opt/archive-backups/时间戳/`。目录权限为 700，文件为 600，不覆盖旧备份。核查备份：`cd /opt/archive-backups/时间戳 && sha256sum -c SHA256SUMS`。恢复时先停止服务，从已核查的同一份备份复制 `.env` 和 `viewer_users.db` 到 `/opt/archive-data`，设置属主 `archive:archive`、权限 600，再启动服务。恢复会回退备份之后创建的用户及授权，因此要先保存现有数据。生产环境还应定期将备份复制到另一台主机；不要把 `.env` 或备份放进代码包。

官群统计默认按群名含“官群”识别。若官群有其他命名，可在 `/opt/archive-data/.env` 设置 `VIEWER_OFFICIAL_GROUP_KEYWORDS=官群,其他关键词`，也可用 `VIEWER_OFFICIAL_GROUP_IDS=群ID1,群ID2` 指定漏识别的群。修改后重启服务，用同一日期抽查官群活跃度列表和客服消息统计结果。两个页面使用同一识别规则。

已有部署首次升级到独立数据目录时，先停止服务，再解压新版程序，然后按以下顺序迁移。**在确认旧库仍存在前，不要初始化新库。**

```bash
sudo systemctl stop archive-viewer
sudo unzip -o /tmp/archive-viewer-ubuntu22.04.zip -d /opt/archive-viewer
sudo chown -R archive:archive /opt/archive-viewer
sudo install -d -o archive -g archive -m 700 /opt/archive-data
sudo -u archive /opt/archive-viewer/.venv/bin/python /opt/archive-viewer/deploy/migrate_user_db.py
sudo nano /opt/archive-data/.env
# 添加：VIEWER_USER_DB=/opt/archive-data/viewer_users.db
sudo cp /opt/archive-viewer/deploy/archive-viewer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start archive-viewer
```

迁移脚本复制旧 `.env`，通过 SQLite 备份接口复制现有账号，拒绝覆盖已有目标，并保留原文件。如同时找到两个旧库，使用 `--db-source` 明确指定。若旧库已丢失，先从服务器备份恢复；无法恢复时需重新创建账号和免登授权。

完成一次迁移后，后续升级使用新的 ZIP 执行 `sudo unzip -o /tmp/archive-viewer-ubuntu22.04.zip -d /opt/archive-viewer`，然后 `sudo chown -R archive:archive /opt/archive-viewer` 和 `sudo systemctl restart archive-viewer`。ZIP 不含 `.env`、用户库、媒体文件或缓存。确认 `/opt/archive-data/.env` 中的 `VIEWER_USER_DB` 指向独立目录。备份 `/opt/archive-data`，其中音频转码缓存可重建。存档消息持续从 PostgreSQL 读取；默认 45 秒的进程内查询缓存只用于提速，重启后自动重建。可在 `.env` 设置 `VIEWER_CACHE_TTL_SECONDS=0` 关闭缓存。

IT小爱免登：在客服系统“设置 → 用户与权限”中，将普通账号的“IT小爱 userId”设为 `EMPLOYEE_ID`，管理员账号设为 `ADMIN_ID`，两者分别生成密钥并配置到 IT小爱服务器。客服系统接受 `X-Aura-SSO-Key` 加 JSON `{"userId":"..."}`，同时校验密钥与绑定 ID；返回 `/?sso_token=...` 一次性链接。原有 `X-SSO-Key` 方式仍可用。若需要检查部署服务器的授权状态及最近审计，可运行 `sudo -u archive /opt/archive-viewer/.venv/bin/python /opt/archive-viewer/deploy/inspect_sso.py --user EMPLOYEE_ID --user ADMIN_ID`。加 `--check-key` 后在终端隐式输入一把长期密钥，脚本只显示匹配与绑定账号，不输出密钥或摘要。每把密钥分别运行一次。不要把密钥放到命令参数、日志或聊天记录中。
