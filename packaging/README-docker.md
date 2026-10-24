# 用 Docker 运行本地 API（`novel-api`）

本目录提供 `novel-api` 的容器化运行方案。镜像只包含 REST API 及其运行依赖
（`api` + `music` extras），适合把下载服务放在 NAS、服务器或独立容器里，
再由 Android 客户端或其他 HTTP 客户端访问。

> 构建上下文必须是**仓库根目录**，不是 `packaging/`。

## 1. 构建

```bash
# 在仓库根目录执行
docker build -f packaging/Dockerfile -t novel-downloader-api:local .
```

- 基础镜像：`python:3.12-slim`
- 安装方式：`pip install -e ".[api,music]"`
- 仓库根的 `.dockerignore` 会排除 `.git`、`.venv`、`build/dist`、`tests`、
  `docs`、`android`、`legacy` 以及本地的 `downloads/`、`settings.toml` 等，
  避免把本地数据和凭据打进镜像。
- 镜像内以非 root 用户运行（UID/GID `10001`）。

## 2. 运行（最小示例）

先准备一个随机 token，并写入权限受限的 env 文件（比直接写在命令行更安全，
命令行参数可能出现在 shell 历史和 `ps` 中）：

```bash
TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
umask 077
printf 'NOVEL_API_TOKEN=%s\n' "$TOKEN" > novel-api.env

docker run -d \
  --name novel-api \
  --restart unless-stopped \
  -p 127.0.0.1:8081:8081 \
  --env-file novel-api.env \
  -v novel-api-data:/data \
  novel-downloader-api:local
```

- `-p 127.0.0.1:8081:8081`：只发布到宿主机回环地址，宿主机之外的设备无法访问。
- `-v novel-api-data:/data`：命名卷，保存配置、状态和下载文件。
- token 通过 `NOVEL_API_TOKEN` 传入，API 会要求
  `Authorization: Bearer <token>` 或 `X-API-Key: <token>`。
- 请只用环境变量传 token：入口脚本和镜像自带的 `HEALTHCHECK` 都读取
  `NOVEL_API_TOKEN`。把 `--token` 作为镜像启动参数传入时，入口脚本不会
  识别它，会按“无 token”处理并拒绝启动。
- 容器内固定监听 `0.0.0.0:8081`；可以用 `docker logs -f novel-api` 查看启动日志。

### 需要让 Android / 局域网设备访问时

把端口发布到所有网卡：

```bash
docker run -d \
  --name novel-api \
  --restart unless-stopped \
  -p 8081:8081 \
  --env-file novel-api.env \
  -v novel-api-data:/data \
  novel-downloader-api:local
```

然后放行宿主机防火墙的 TCP 8081（Windows 示例，管理员 PowerShell/CMD）：

```bat
netsh advfirewall firewall add rule name="Novel API 8081" dir=in action=allow protocol=TCP localport=8081
```

**不要**在未设置 token 的情况下执行 `-p 8081:8081`。

## 3. 安全默认：没有 token 拒绝启动

容器内的 API 绑定 `0.0.0.0`，因此入口脚本
（`packaging/docker-entrypoint.sh`）把 token 视为必需项：

- 未设置 `NOVEL_API_TOKEN`（且未通过 `--token` 传入）时，打印醒目错误并
  **拒绝启动**（退出码非 0）。
- 只有在显式设置 `NOVEL_API_ALLOW_NO_TOKEN=1` 时才允许无鉴权启动；此时会
  持续打印警告。仅限完全可信的隔离网络使用。
- 入口脚本不会打印 token 本身。

```bash
# 明确放弃鉴权（不推荐）
docker run ... -e NOVEL_API_ALLOW_NO_TOKEN=1 ...
```

额外的安全边界：

- `/api/v1/*` 受 token 保护；`/downloads/*` 是静态文件挂载，**不受 token
  保护**，能访问该端口的人就可以读取导出文件。
- `/docs`、`/openapi.json` 也不在 token 依赖内。
- 默认不启用 CORS（`--cors` 也未传入）。需要时可在镜像名后追加参数，例如
  `... novel-downloader-api:local --cors`，但不要与公网暴露同时使用。
- 如需从公网访问，请在前面加 TLS 反向代理并额外做访问控制，而不是直接
  发布 8081。

## 4. 健康检查

`/api/v1/health` 位于 token 依赖之下，因此设置了 token 时请求必须带上它：

```bash
curl -fsS -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8081/api/v1/health
# {"status":"ok","version":"..."}
```

未设置 token（`NOVEL_API_ALLOW_NO_TOKEN=1`）时：

```bash
curl -fsS http://127.0.0.1:8081/api/v1/health
```

镜像自带 `HEALTHCHECK`（每 30 秒用容器内 Python 探测一次，并自动带上
`NOVEL_API_TOKEN`）。查看状态：

```bash
docker inspect --format '{{.State.Health.Status}}' novel-api
# healthy / starting / unhealthy
```

注意：`HEALTHCHECK` 固定探测容器内的 `8081` 端口，不要把 `--port` 作为额外
参数传进去。

## 5. 数据卷 `/data`

容器把所有可写数据都放在 `/data`（`NOVEL_HOME=/data`）：

| 路径 | 内容 |
| --- | --- |
| `/data/settings.toml` | 配置文件（存在时自动加载） |
| `/data/state.json` | 应用状态 |
| `/data/watchlist.json` | 追更列表 |
| `/data/novel_cache/` | 缓存（`NOVEL_CACHE_DIR`） |
| `/data/downloads/` | 导出文件（`general.output_dir`），通过 API 的 `/downloads/*` 提供下载 |

容器的工作目录是 `/data`，所以：

- 配置查找会优先命中 `/data/settings.toml`；
- 配置里的相对路径（默认 `output_dir = "./downloads"`）会落在
  `/data/downloads`，即卷内。若想显式固定，写入：

```toml
[general]
output_dir = "/data/downloads"
```

- **只有 `/data` 会被持久化**。配置里指向 `/data` 之外的绝对路径在容器重建后
  会丢失。

### 使用宿主机目录（bind mount）

```bash
mkdir -p ./docker-data
# Linux 上需要把目录交给容器内的 UID 10001；Windows Docker Desktop 通常无需手动处理
sudo chown -R 10001:10001 ./docker-data

docker run -d \
  --name novel-api \
  -p 127.0.0.1:8081:8081 \
  --env-file novel-api.env \
  -v "$PWD/docker-data:/data" \
  novel-downloader-api:local
```

如果容器日志提示无法创建 `/data/downloads` 或 `/data/novel_cache`，基本都是
宿主目录权限问题（容器以 UID/GID 10001 运行）。

### 放入自己的 `settings.toml`

可以从镜像里取出示例配置，编辑后以只读文件挂载：

```bash
docker run --rm --entrypoint cat novel-downloader-api:local \
  /app/src/novel_downloader/resources/config/settings.sample.toml > settings.toml

# 编辑 settings.toml（站点 cookies、输出目录等）后：
docker run -d \
  --name novel-api \
  -p 127.0.0.1:8081:8081 \
  --env-file novel-api.env \
  -v "$PWD/settings.toml:/data/settings.toml:ro" \
  -v novel-api-data:/data \
  novel-downloader-api:local
```

### 备份卷

```bash
docker run --rm -v novel-api-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/novel-api-data.tgz -C /data .
```

## 6. 客户端接入

### Android 客户端

1. API 容器按上文使用 `-p 8081:8081` 发布到局域网，并设置 token；
2. App → 右上角「设置」：
   - 模拟器（API 跑在宿主机）：`http://10.0.2.2:8081/`
   - 真机：`http://<宿主机局域网 IP>:8081/`
   - 「API 密钥」填入 `NOVEL_API_TOKEN` 的同一值；
3. 手机与宿主/容器所在主机必须在同一网络，且防火墙放行 8081。

### 桌面端

`novel-desktop` 是本机 GUI，直接调用同一进程内的服务层，**没有“连接远程
REST API”的模式**；它状态栏里的 `http://127.0.0.1:8081` 只是展示默认 API
地址。因此桌面端不能作为该容器的远程客户端：请继续在宿主机上运行
`novel-desktop`，或改用 Android / 任意 HTTP 客户端访问容器。

### 在容器内使用 CLI

镜像安装了完整的控制台入口，可以 `docker exec` 运行 CLI 子命令：

```bash
docker exec -it novel-api novel-cli --help
```

CLI 的工作目录同样是 `/data`，会读写同一个数据卷。

## 7. 已知限制

1. **无 GUI**：镜像只运行 `novel-api`；桌面端不包含在镜像内。
2. **任务队列在内存中**：容器/进程重启后，排队中或运行中的任务会丢失；
   已写入 `/data/downloads` 的文件仍在。
3. **单实例**：任务管理器是进程内单例，不要对同一数据卷启动多个副本。
4. **`/downloads` 无鉴权**：即使设置了 token，静态导出目录仍可被直接读取；
   公网暴露时必须依赖反向代理、VPN 或网络 ACL。
5. **配置与绝对路径**：只有 `/data` 持久化，且容器以 UID 10001 运行；
   bind mount 的权限/属主需要匹配。
6. **出站网络**：下载站点和音乐平台由容器直接访问，需要允许容器出站；
   `music` extras 已安装，但没有图形/浏览器登录辅助工具。
7. **端口固定**：入口脚本与健康检查都以 `8081` 为准。