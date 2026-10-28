# 阿里云 ECS 部署

推荐结构：阿里云 ECS → Nginx/HTTPS → Docker 容器 → `/data` 持久化卷。应用只监听 ECS 本机的 `127.0.0.1:8080`，公网流量由 Nginx 接收。

## 1. ECS 安全组

只开放：

- `22/tcp`：仅你的固定 IP，用于 SSH；
- `80/tcp`、`443/tcp`：如果使用域名和 HTTPS；
- 不要把 `8080` 开放到公网。

在还没有登录、用户隔离和限流之前，不建议把安全组设置为所有来源都可访问。

## 2. 服务器准备

在 ECS 上安装 Docker 和 Compose 插件，然后把项目放到服务器目录，例如 `/opt/co-scientist`。不要把 `.env` 提交到 Git：

```bash
cd /opt/co-scientist
cp config/deployment.example.toml config/deployment.toml
cp .env.example .env  # 如果项目中没有该文件，则手动创建
chmod 600 .env config/deployment.toml
```

在 `.env` 中只填写服务器端 Provider 所需的 Key，例如 `OPENROUTER_API_KEY`。不要把 Key 写进 TOML、前端或浏览器。

## 3. 启动应用

```bash
cd /opt/co-scientist
docker compose -f docker-compose.example.yml up -d --build
docker compose -f docker-compose.example.yml logs -f co-scientist
curl http://127.0.0.1:8080/healthz
```

第一次启动会执行幂等数据库迁移。Compose 卷 `co_scientist_data` 保存数据库、上传文件、任务记录和 Evidence 原始文件。

## 4. 域名和 HTTPS

将域名 A 记录指向 ECS 公网 IP，安装 Nginx，将 [nginx.conf.example](../deployment/nginx.conf.example) 复制到站点配置并替换域名。再使用 Certbot 或阿里云证书服务签发证书，并将 HTTP 重定向到 HTTPS。

Nginx 配置中的 `proxy_buffering off` 对 SSE Agent 活动流很重要；较长的读写超时用于 Supervisor 的长任务。上传大小应与应用的 30 MiB 分组上限保持一致。

## 5. 上线前验证

```bash
curl -fsS https://your-domain.example/healthz
```

先用 Mock 或极低预算创建一个测试任务，确认：上传、Research Brief、Session 创建、SSE 活动、暂停和错误提示都正常，再开放给朋友。AlphaProtein Novo 真实计算仍保持关闭，除非 GPU Worker、模型权重、许可和人工审核已经单独配置。

## 重要限制

当前项目尚未提供完整的用户登录、组织成员权限、每用户配额和公网攻击防护。因此这套部署目前适合个人或受控课题组内测，不应直接作为开放注册的公共网站。下一阶段应增加身份认证、Session 所有权校验、反向代理限流、备份恢复和独立队列 Worker。

## Docker Hub 无法访问时：Python 3.11 + systemd

Alibaba Cloud Linux 3 的 `docker` 软件包可能提供 Podman 兼容命令；本机实际检测为 Podman 4.9.4、podman-compose 1.0.6。若官方 Python 镜像下载超时，可直接使用系统仓库的 Python 3.11，保留 Docker 文件供后续环境使用。

以下操作适用于项目已上传到 `/opt/co-scientist` 的新服务器：

```bash
dnf install -y python3.11 python3.11-pip nginx
cd /opt/co-scientist
python3.11 -m venv .venv
.venv/bin/pip install . --index-url https://mirrors.aliyun.com/pypi/simple/
# 已有 deployment.toml 或 .env 时保留它们，勿重复覆盖。
cp -n config/deployment.example.toml config/deployment.toml
sed -i 's/allow_live_calls = true/allow_live_calls = false/' config/deployment.toml
touch .env
chmod 600 .env
id coscientist || useradd --system --home-dir /opt/co-scientist --shell /sbin/nologin coscientist
chown coscientist:coscientist .env
install -d -m 0700 -o coscientist -g coscientist /data
install -m 0644 deployment/co-scientist.service /etc/systemd/system/co-scientist.service
systemctl daemon-reload
systemctl enable --now co-scientist
systemctl status co-scientist --no-pager
curl -fsS http://127.0.0.1:8080/healthz
```

systemd 使用独立服务用户，启动前执行数据库迁移，失败时有限重启，持久化数据仍位于 `/data`。维护命令为 `systemctl restart co-scientist` 和 `journalctl -u co-scientist -n 100 --no-pager`。

`deployment/nginx-ip-preview.conf` 提供临时公网 IP 预览配置，安装前应先检查现有 Nginx 站点，避免覆盖已有服务。它未启用 HTTPS 或身份认证，仅用于无密钥、无私有科研数据的初始预览。真实 API Key 配置、付费推理验证及课题组访问保护应在 HTTPS 和认证完成后单独开展。

安装 IP 预览配置时，将 `server_name _;` 改成 `server_name 你的公网IP;`，避免请求被系统自带的默认站点接收。执行 `nginx -t` 通过后再启动 Nginx。

从 macOS 打包时建议使用 `COPYFILE_DISABLE=1 tar --no-xattrs ...`，避免 AppleDouble 隐藏文件随源码上传。数据库迁移发现逻辑也会忽略隐藏的 `.sql` 文件，相关回归测试位于 `co_scientist/tests/unit/test_deployment_migrations.py`。
