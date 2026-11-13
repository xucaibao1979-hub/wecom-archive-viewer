# Ubuntu 22.04 安装部署

本项目是独立的会话存档**查看器**。代码部署在 `/opt/archive-viewer`；配置和账号库保存在 `/opt/archive-data`；会话正文读取外部 PostgreSQL，媒体从 `VIEWER_MEDIA_ROOT` 读取。代码包不含这些生产数据。升级、维护、备份和恢复见 [运维手册](OPERATIONS.md)。

## 1. 准备服务器与代码

服务器需能访问存档 PostgreSQL，域名需解析到服务器。对外开放 80/443；Gunicorn 仅监听 `127.0.0.1:8765`。

从本机上传不含 `.env`、用户库和媒体的部署 ZIP：

```powershell
scp .\archive-viewer-YYYYMMDD.zip user@服务器IP:/tmp/
```

在 Ubuntu 执行：

```bash
sudo apt update
sudo apt install -y curl unzip nginx snapd
sudo useradd --system --create-home --shell /bin/bash archive
sudo install -d -o archive -g archive -m 755 /opt/archive-viewer
sudo install -d -o archive -g archive -m 700 /opt/archive-data
sudo unzip /tmp/archive-viewer-YYYYMMDD.zip -d /opt/archive-viewer
sudo chown -R archive:archive /opt/archive-viewer
```

`archive-viewer-YYYYMMDD.zip` 应替换为实际文件名。若 `archive` 用户已存在，跳过 `useradd`。不要把旧的 `.env` 或用户库解压到代码目录。

## 2. 安装 Python 与依赖

```bash
sudo -u archive -H bash -lc 'curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u archive -H bash -lc 'cd /opt/archive-viewer && ~/.local/bin/uv python install 3.12 && ~/.local/bin/uv venv --python 3.12 .venv && ~/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt gunicorn'
```

## 3. 填写配置、创建管理员

```bash
sudo cp /opt/archive-viewer/.env.example /opt/archive-data/.env
sudo nano /opt/archive-data/.env
```

至少填写 `WECOM_DB_*`、`VIEWER_SECRET_KEY`、`VIEWER_ADMIN_USER`、`VIEWER_ADMIN_PASSWORD`、`VIEWER_PUBLIC_ORIGIN=https://实际域名`；保持 `VIEWER_USER_DB=/opt/archive-data/viewer_users.db` 和 `VIEWER_COOKIE_SECURE=true`。设置媒体目录 `VIEWER_MEDIA_ROOT`；若媒体在另一台主机，先挂载并赋予 `archive` 用户读取权限。数据库账号建议只授予存档表的读取权限。`VIEWER_SECRET_KEY` 可用 `openssl rand -hex 32` 生成，勿写入代码仓库。

```bash
sudo chown archive:archive /opt/archive-data/.env
sudo chmod 600 /opt/archive-data/.env
sudo -u archive bash -lc 'cd /opt/archive-viewer && .venv/bin/python -c "import app; app.bootstrap_admin()"'
```

只在**全新部署**时初始化管理员。此后密码、用户、菜单权限和免登授权均存入 `/opt/archive-data/viewer_users.db`；修改 `.env` 的初始密码不会更改已有账号。已有数据时不要创建新库，参见 [旧部署迁移](OPERATIONS.md#旧部署迁移)。

## 4. 启动服务

```bash
sudo cp /opt/archive-viewer/deploy/archive-viewer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now archive-viewer
sudo systemctl status archive-viewer --no-pager
curl -I http://127.0.0.1:8765/
```

`curl` 返回登录跳转或其他 HTTP 响应说明本机监听已启动；若连接拒绝，先查看 `sudo journalctl -u archive-viewer -n 100 --no-pager`。

## 5. Nginx 和 HTTPS

将示例配置中的 `archive.example.com` 替换为实际域名：

```bash
sudo cp /opt/archive-viewer/deploy/archive-viewer.nginx.conf /etc/nginx/conf.d/archive-viewer.conf
sudo nano /etc/nginx/conf.d/archive-viewer.conf
sudo nginx -t
sudo systemctl reload nginx
sudo snap install core
sudo snap refresh core
sudo snap install --classic certbot
sudo ln -s /snap/bin/certbot /usr/bin/certbot
sudo certbot --nginx -d 实际域名
```

确认 HTTP 自动跳转 HTTPS。服务端的 Secure Cookie 只会在 HTTPS 下保存，因此不要以 HTTP 局域网地址作为正式登录入口。Nginx 示例日志只记录路径、不记录免登票据的查询参数；若还有其他代理或 WAF，也应遮盖票据参数。长期免登密钥仅保存在调用方服务端。

## 6. 上线核验

访问 `https://实际域名`，用首次创建的管理员账号登录。依次检查客户列表、群聊记录、图片或文件预览、普通用户的菜单权限，并在需要时测试 IT小爱的免登入口。免登接口契约与安全约束见项目 [README](../README.md#用户专属免登密钥与一次性票据)。

随后按 [运维手册](OPERATIONS.md#每日备份与保留-30-天) 配置每日备份，并将 PostgreSQL 和媒体目录纳入各自的备份计划。
