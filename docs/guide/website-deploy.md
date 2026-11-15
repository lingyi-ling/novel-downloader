# 官网静态站部署

书音官网是 `website/` 下的纯静态站点：没有构建步骤，也没有包管理。线上由 nginx 从服务器的 `/var/www/noveldl` 提供服务，入口是 <http://43.165.170.43/noveldl/>。部署脚本 `scripts/deploy_site.py` 只做文件同步，**不修改 nginx 配置**。

## 部署清单

`scripts/deploy_site.py` 中的 `SITE_FILES` 是显式允许清单，只有列出的文件会被上传：

| 文件 | 用途 |
| --- | --- |
| `index.html` | 首页 |
| `404.html` | 静态 404 页 |
| `main.js` | 交互脚本 |
| `style.css` | 样式 |
| `favicon.svg` | 站点图标 |
| `og-image.png` | 社交分享图 |
| `robots.txt` | 爬虫规则 |
| `sitemap.txt` | 站点地图 |

`404.html` 已在清单内，会随每次部署上传。`website/` 下新增任何文件都必须同步加入 `SITE_FILES`，否则不会被发布；`--dry-run` 会打印本次将上传的文件名，可先用来确认清单。

## 执行部署

```bash
# 只列出将上传的文件，不连接服务器
.venv/Scripts/python.exe scripts/deploy_site.py --dry-run

# 实际上传
.venv/Scripts/python.exe scripts/deploy_site.py
```

脚本先把文件传到服务器 `/tmp` 的临时目录，逐个检查存在后再组装新站点：新目录以当前 docroot 的副本为底，覆盖本轮上传的文件，最后用两次 rename 原子地替换 `/var/www/noveldl`。替换失败会恢复旧目录，因此不会留下半个站点。远程步骤使用 `sudo -n`，要求部署用户对 docroot 有免密 sudo 权限。

依赖 `paramiko` 时才需要安装部署附加项：

```bash
pip install -e .[deploy]
```

认证优先使用 SSH key/agent；没有 key 时才读取环境变量 `NOVELDL_DEPLOY_PASSWORD`，密码不会写入文件、日志或命令行。

## 为什么上传了 404.html 却不生效

把 `404.html` 传上去，只代表文件存在于服务器的 `/var/www/noveldl/404.html`。nginx 遇到不存在的路径时默认返回自己的内置 404 页面，**不会自动读取 docroot 里的同名文件**。必须在服务器侧的 nginx 配置中显式指定错误页，例如放在对应站点的 `server` 块里：

```nginx
error_page 404 /noveldl/404.html;

location = /noveldl/404.html {
    internal;
}
```

- `error_page` 的第二个参数是 **URI**，不是文件系统路径；站点挂在 `/noveldl/` 前缀下，所以要写 `/noveldl/404.html`。
- `location = /noveldl/404.html { internal; }` 让该地址只能由 nginx 内部错误处理使用：客户端直接请求它不会以 `200` 返回页面，而仍是 `404`。
- 这份配置**不在本仓库内**。当前部署对应服务器上的 `/etc/nginx/sites-available/parfait.conf`。`scripts/deploy_site.py` 只有 docroot 的免密 sudo 权限，只替换 `/var/www/noveldl` 下的文件，既没有权限也不会去改 nginx。
- 修改后先校验语法，再 reload；这一步需要服务器管理员权限，不能由部署脚本代做：

  ```bash
  sudo nginx -t
  sudo systemctl reload nginx
  ```

- 这是服务器侧的一次性配置。之后每次部署只替换 docroot 内容，不会覆盖或撤销它；反过来说，重新部署也不会替你补上这段配置。

## 验证

配置生效后，任意不存在的路径都应返回 `404`，且响应体是 `website/404.html` 的内容。只用 `curl -I` 只能看到状态码，无法证明 body 是自定义页，建议同时检查下面几项：

```bash
# 1) 不存在的路径：状态码必须是 404
curl -s -o /dev/null -w '%{http_code}\n' \
  http://43.165.170.43/noveldl/__no_such_page__

# 2) 响应体必须是自定义 404 页
curl -s http://43.165.170.43/noveldl/__no_such_page__ | grep -F "404 · 页面未找到"

# 3) 逐字节核对线上错误页与源码一致
curl -s http://43.165.170.43/noveldl/__no_such_page__ -o /tmp/live-404.html
cmp /tmp/live-404.html website/404.html && echo "404 page OK"

# 4) internal 生效：直接请求 404.html 仍返回 404（不是 200）
curl -s -o /dev/null -w '%{http_code}\n' \
  http://43.165.170.43/noveldl/404.html
```

如果第 1 步得到 `404`、第 2 步却匹配不到自定义页内容，通常说明 nginx 没有加载 `error_page`。确认配置写在了正确的 `server` 块、已经 reload，并查看运行中的配置：

```bash
sudo nginx -T | grep -n "error_page"
```

首页仍应逐字节一致：

```bash
curl -s http://43.165.170.43/noveldl/index.html -o /tmp/live-index.html
cmp /tmp/live-index.html website/index.html && echo "index OK"
```

## 与发布流水线的关系

`website/` 有改动时，按仓库约定在门禁通过后运行 `scripts/deploy_site.py`，再用上面的 `curl` / `cmp` 命令核对线上内容。版本号、三端构建和 GitHub release 由 `scripts/release.py` 负责，与本静态站部署是两条独立流程。