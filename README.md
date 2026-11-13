# 本地会话存档查看器

独立部署的只读 Web 应用，通过 PostgreSQL 查询企业微信会话存档的 `messages`、`media_files` 表。无需会话存档采集项目的源码或配置文件。

## 部署与运维文档

- [Ubuntu 22.04 首次安装部署](deploy/UBUNTU-22.04.md)
- [升级维护、每日备份、30 天保留与恢复](deploy/OPERATIONS.md)
- [每日代码与本地持久化数据备份脚本](deploy/backup_project.py)

查看器备份包不含外部 PostgreSQL 会话数据库及 `VIEWER_MEDIA_ROOT` 媒体目录；这两部分需单独备份。

## 启动

安装 Python 3.11+ 和 uv。Ubuntu 部署时在 `/opt/archive-data/.env` 填写数据库连接（参见 `.env.example`）；Windows 本地运行仍可用项目目录的 `.env`，然后启动：

```powershell
uv run --with-requirements requirements.txt python app.py
```

打开 <http://127.0.0.1:8765>。可用 `VIEWER_PORT` 修改端口。数据库账号至少需要 `messages` 和 `media_files` 的 SELECT 权限，建议使用专用只读账号。程序连接时也会启用 PostgreSQL 只读事务。

首次启动时使用 `.env` 的 `VIEWER_ADMIN_USER`、`VIEWER_ADMIN_PASSWORD` 创建管理员账号。Ubuntu 的登录用户保存在 `/opt/archive-data/viewer_users.db`（SQLite）中；之后修改 `.env` 初始密码不会重置已有账号。媒体文件存放在本机或挂载目录时，设置 `VIEWER_MEDIA_ROOT` 指向该目录。

升级代码时保留服务器上的 `/opt/archive-data`，不要删除或覆盖其中的 `.env` 和用户库；存档消息仍保存在原 PostgreSQL，媒体文件仍在 `VIEWER_MEDIA_ROOT`。部署 ZIP 不包含这些数据。音频转码缓存也位于 `/opt/archive-data/cache/audio`，可重建。常用列表及统计查询使用进程内短时缓存，默认 45 秒，可通过 `VIEWER_CACHE_TTL_SECONDS` 调整为 0–300 秒（0 表示关闭）。缓存不存储原始数据，也不影响持久化记录；消息正文、文件下载和导出始终从数据库或媒体目录读取。

## 当前范围

- 会话列表由最近 10,000 条消息推导，最多显示 300 个会话。
- 双人会话依据发送人和接收人配对，使用 `wecom_users` 区分员工与客户；群聊依据 `roomid` 分组。无双人配对的非群聊消息暂不进入会话列表。
- 存档员工、会话列表、客户列表和客户群列表默认每页 20 条，可切换每页 50/100 条，选择保存在浏览器中。
- 进入群聊会话或存档员工菜单时自动选中首项，聊天记录默认显示所选日期内最新 20 条且按时间从上到下排列；右下角支持 10/20/50/100/500 条每页、自定义 1–500 条、全部分批加载和页码跳转，可用“上一页”“下一页”翻页，也可按日期、发送人、类型和关键词筛选。
- 消息读取 `raw_json` 展示链接、位置等内容；图片、音频、视频和文件通过登录后的媒体接口访问，文件需位于 `VIEWER_MEDIA_ROOT` 内。
- 可导出完整会话的单文件 HTML（含结构化 JSON、离线关键词与成员筛选、引用样式，以及已保存到本地的图片），也可导出 JSON 或 CSV。JSON 使用 `version`、`title` 和 `messages`；每条消息包含 `id`、`sender`（`id`、`name`）、`timestamp`、`type`、`content`、`quote`、`attachments`、`recalled`。群文件支持搜索、日期筛选和所选文件批量打包下载。未保存到服务器媒体目录的文件会列在 ZIP 的说明文件中。
- 群文件默认筛选最近 7 天，群列表按最新消息排序，并展示期间消息数和文件数。会话导出可独立指定开始和结束日期；图片优先从本地媒体目录嵌入，缺失时仅从 `VIEWER_EXPORT_IMAGE_HOSTS` 指定的 HTTPS OSS 域名读取并嵌入。超大或无法读取的图片保留原链接，打开 HTML 时需要网络。
- 存档员工的群列表按 `wecom_rooms` 中的成员关系确定；员工列表显示所在群数及本人发送的群消息数。
- 群聊会话按群内最新消息时间排序；会话导出默认按所选日期的群消息数排序，可点击“所选日期群消息”或“总消息”标题切换排序。导出文件仍包含该群完整聊天记录。
- 数据统计提供客户数据和会话数据。客户页按客户、客户群各展示四个指标；累计趋势基于当前外部群成员记录的首次入群时间。流失客户与解散群需要历史快照，当前数据库无法计算。
- 客服工具包含官群活跃度和客服消息统计，可按日期、日/月维度查询并导出当前结果。官群默认按当前群名包含“官群”识别；可在 `.env` 用 `VIEWER_OFFICIAL_GROUP_KEYWORDS` 配置逗号分隔的关键词，并用 `VIEWER_OFFICIAL_GROUP_IDS` 补充未命中关键词的群 ID。两个统计页面共用此规则；修改配置后重启服务。每次调整规则后，建议用相同日期在官群活跃度列表抽查群名及消息数，并与客服消息统计中的官群数核对。历史群名变化不会自动还原，需以当前群资料核实。管理员在“设置 → 客服消息统计”点击“修改”后可添加、全选、移除人员并保存；恢复默认后按 `.env` 中的 `VIEWER_SERVICE_DEPARTMENT_IDS` 部门编号筛选，多个编号用逗号分隔，未配置时默认使用 `194`。
- 密码登录每 5 分钟限制同一来源 40 次、同一用户名 12 次尝试；达到上限时返回 429，五分钟后可重试。免登流程保持原有规则。

### 用户专属免登密钥与一次性票据

本功能不依赖企业微信身份。管理员在“设置 → 用户与权限”为账号分配角色和菜单权限，并生成免登密钥。密钥使用至少 32 字节安全随机数，明文只显示一次；本地 SQLite 仅保存 SHA-256 摘要、授权版本和生成时间。管理员可以重置或吊销授权。普通用户不能查看设置或调用用户管理接口。

正式部署时在 `.env` 设置 `VIEWER_PUBLIC_ORIGIN=https://archive.example.com`、`VIEWER_COOKIE_SECURE=true`，并将 `VIEWER_SECRET_KEY` 设置为至少 32 字节随机值。使用 HTTPS 反向代理转发到本机 8765 端口。未配置 HTTPS 地址和 Secure Cookie 时，换票接口返回 503；本地密码登录仍可用。

调用方后端根据自己已验证的当前用户选择对应长期密钥，立即调用：

```http
POST /api/sso/ticket HTTP/1.1
Host: archive.example.com
X-SSO-Key: <当前用户对应的长期免登密钥>
```

IT小爱现有请求方式也可直接使用：请求头 `X-Aura-SSO-Key: <对应密钥>`，JSON 请求体 `{"userId":"EMPLOYEE_ID"}`（管理员使用配置的 `ADMIN_ID`）。管理员先在“设置 → 用户与权限”分别将 `EMPLOYEE_ID`、`ADMIN_ID` 绑定到普通和管理员本地账号。客服系统同时检查密钥与其绑定的 `userId`，不允许仅凭请求体选择账号或管理员角色。两种请求头同时存在且密钥不同会被拒绝。IT小爱请求成功返回 `{"loginUrl":"https://archive.example.com/?sso_token=<一次性票据>","expiresIn":60}`，浏览器访问后立即核销并 303 跳转首页。原有 `X-SSO-Key` 接口继续有效，返回 `/sso/login?ticket=...`。

缺少密钥请求头或 IT小爱 `userId` 格式错误返回 400；密钥未匹配、绑定不符、账号停用或授权失效返回 403。服务端审计日志区分这些原因，不记录密钥或摘要。可在部署服务器用 `deploy/inspect_sso.py --user EMPLOYEE_ID --user ADMIN_ID` 查看绑定状态，或加 `--check-key` 在终端隐式输入密钥检查绑定账号；不要把密钥放进命令参数或聊天记录。

成功返回 `{"loginUrl":"https://archive.example.com/sso/login?ticket=<一次性票据>","expiresIn":60}`。调用方仅将 `loginUrl` 交给当前用户浏览器打开；长期密钥只保存在调用方服务端，不能放入网页、URL 或日志。服务端调用示例：

```sh
curl -X POST https://archive.example.com/api/sso/ticket -H "X-SSO-Key: $SSO_KEY"
```

查看器在 `viewer_users.db` 中使用原子 `DELETE ... RETURNING` 消费票据。票据有效期 60 秒且只能使用一次，并绑定用户和授权版本。过期、重复使用、账号停用、密钥重置或吊销都会拒绝。成功后设置正常登录会话 Cookie，并以 HTTP 303 跳转首页，从地址栏移除票据。角色和菜单权限在每次请求时从数据库读取。禁用账号立即阻止所有会话继续访问；重置或吊销授权立即终止旧免登会话，原有密码会话继续有效。免登会话最长 8 小时。

同一台主机上的多个进程可共享 `viewer_users.db` 并原子消费票据；多主机部署必须让所有实例使用同一个事务型认证数据库，不能各自使用本地副本。`sso_rate_limits` 表实施换票和核销限流，`audit_events` 记录生成、重置、吊销、换票、登录成功及失败，不记录密钥或完整票据。反向代理应遮盖 `/?sso_token=` 和 `/sso/login?ticket=` 查询参数，调用方也不要记录完整 `loginUrl`。票据响应与登录跳转设置 `no-store` 和 `no-referrer`。会话 Cookie 为 HttpOnly、Secure、SameSite=Lax，浏览器写操作需要 CSRF token。登录地址只由可信配置生成，不接收外部 returnUrl。

部署后验证密码登录、票据过期、重复和并发消费、授权重置或吊销、账号停用、普通用户越权，以及反向代理的 HTTPS 和 Cookie 行为。
- 登录后默认打开企业概况首页；点击左上角“会话存档”可随时返回。首页展示存档客户沟通、活跃客户群、员工发言排行和趋势；风险与超时没有独立事件表，暂不显示数字。
- 默认仅监听本机 `127.0.0.1`。如需让其他设备访问，应另行配置反向代理、身份验证和 HTTPS。
