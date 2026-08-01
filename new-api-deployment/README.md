# V1A New API deployment template

状态：**最小 VPS 配置模板，未部署、未连接真实域名/账号/Key、未完成 VPS→上游或备份恢复演练**。仓库内的 `.env.example`、Compose 和 Caddyfile 可开源；运行时 `.env`、数据库卷和备份必须留在私有部署环境，不能提交。

边界固定为：

| 层 | 允许 | 禁止 |
|---|---|---|
| 公网 Caddy | 四条 Friend 路由，全部代理到 `friend-gateway` | 管理接口、数据库、`/healthz`、`/v1/models`、`/v1/responses` 和未知路径 |
| Friend gateway | mock 或显式 reference proxy；只向 New API 转发 `POST /v1/messages` | 客户端目录、account/install 覆盖、生成请求自动重试 |
| New API | 仅加入 Docker backend 网络，由 gateway adapter 访问 | 宿主机端口发布或被 Caddy 盲转 |
| MySQL | backend 私网，供 New API 使用 | 宿主机端口发布或公网访问 |
| V1 上游 | `LowcostAI` 名称占位 | 真实上游域名、账号、Key、第二线路和自动生成 POST 重试 |

`backend` 网络设为 Docker `internal`，Compose 只把 Caddy 的 `443` 发布到宿主机；Caddy 关闭 admin API，并对四条路径做方法+精确路径匹配，默认返回 404。管理动作应通过私网或 SSH 隧道进入，不通过公网 Caddy 路由。证书签发方式、真实域名、镜像版本和 New API 的厂商变量名必须在部署前由运维人员审阅并写入私有 `.env`，本模板不把“可启动配置”冒充“已部署”。

Friend gateway 的本地 fixture 位于 `../friend-gateway/examples/`，只含 mock catalog 和 key hash；`FRIEND_GATEWAY_MODE=mock` 便于无真实凭据联调。切到 `proxy` 前必须提供服务端绑定文件、审阅过的 catalog、New API 内网 URL 和 balance adapter URL，并完成真实 New API/VPS 证据；本 Compose 接线本身不构成生产适配证据。

## 使用前检查

```bash
cp .env.example .env
# 在私有机器上填写 approved host、已审核镜像和运行时密钥；不要把 .env 带回仓库。
bash scripts/validate-env.sh
bash scripts/validate-config.sh
```

`validate-env.sh` 不 source 任意 shell 语句，只接受受限的 `KEY=value` 行，并拒绝模板占位符。`validate-config.sh` 运行 Compose 静态解析并检查 Caddy 路径白名单、New API/DB 无宿主机端口。

## 健康与恢复

健康脚本只需要无密钥的公网预检和一个 URL：

```bash
bash scripts/healthcheck.sh https://<approved-host>
```

它要求预检返回 `available=true`，并探测管理及后续未启用路径不能得到 2xx；不会发送 Friend Key，也不会宣称上游流式、工具或计费链路已经通过。

备份脚本把 MySQL dump 与 Caddy/Compose 版本文件放进权限为 600 的私有归档，不备份 `.env`：

```bash
bash scripts/backup.sh
RESTORE_CONFIRM=YES bash scripts/restore.sh /var/backups/friend-v1a/friend-v1a-<timestamp>.tar.gz
```

恢复脚本只恢复数据库；配置文件仅供审阅，不会被脚本覆盖。备份目录、RPO ≤ 24 小时、RTO ≤ 4 小时和 VPS→上游 DNS/TLS/超时/流式/工具证据仍需在真实环境单独演练，本目录没有这些部署证据。
