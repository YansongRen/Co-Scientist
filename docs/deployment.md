# 网络部署准备

本项目现在可以用单个容器启动 FastAPI 网页、SQLite 数据目录和现有任务调度。部署层不会改变六类 Agent、Supervisor、Research Scoping 或 Scientific Evidence Layer。

## 部署前必须确认

- 当前没有完整的用户登录、项目级 ACL 和每用户配额；未加认证前只适合私有网络或受保护的内部环境。
- API Key 只通过环境变量传给服务端，不要写入 `deployment.toml`、Dockerfile 或前端。
- AlphaProtein Novo 真实计算默认关闭；没有 GPU、权重和人工审批时不要打开 `allow_real_runs`。
- SQLite 数据必须挂载持久化卷，不能使用容器临时文件系统。
- 当前 `scripts/scoping_preview.py` 是 Mock 预览，不是生产启动入口。

## 本地容器试运行

```bash
cp config/deployment.example.toml config/deployment.toml
# 按实际 Provider 修改 deployment.toml；将 API Key 放在 .env
docker compose -f docker-compose.example.yml up --build
```

浏览器打开 `http://127.0.0.1:8080/#new`。第一次启动会执行幂等数据库迁移，数据保存在 `co_scientist_data` 卷中。

## 云平台启动命令

使用本仓库的 `Dockerfile`，容器启动命令不需要额外覆盖。平台只需要提供：

- `CO_SCIENTIST_CONFIG=/app/config/deployment.toml`
- `PORT`（平台通常自动注入）
- 与 `[llm].provider` 对应的 API Key，例如 `OPENROUTER_API_KEY`

将 `config/deployment.toml` 作为只读 Secret/File 挂载，并将 `/data` 绑定到持久化磁盘。平台健康检查使用 `GET /healthz`。

## 上线前检查清单

1. 用独立的测试数据库完成 `co-scientist init` 和一次 Mock/小预算运行。
2. 确认 `/healthz`、上传大小限制、预算上限和错误日志不包含 API Key。
3. 在反向代理或平台层启用 HTTPS，并增加登录/访问控制。
4. 配置数据库备份；不要把 SQLite 文件放在临时磁盘。
5. 先保持 AlphaProtein Novo 关闭，确认 GPU Worker、权重许可和人工审核流程后再单独接入。
6. 设置 API 供应商侧的硬额度；应用内预算是防护措施，不是供应商账单保证。
