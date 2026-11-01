# AGENTS.md — 本仓库的操作约定

面向在本仓库工作的自动化代理（也适用于人类维护者）。用户已授权：**每次完成工作后自动执行「校验 → 提交 → 推送 → 上官网」**，无需逐步确认。

## 收尾流水线（默认全自动）

```bash
# 1. 门禁（缺一不可）
.venv/Scripts/python.exe -m pytest tests -q
.venv/Scripts/python.exe -m ruff check src tests scripts
.venv/Scripts/python.exe -m ruff format --check src tests scripts
.venv/Scripts/python.exe -m mypy src

# 2. 若改动涉及用户可见字符串
.venv/Scripts/python.exe scripts/update_pot.py
.venv/Scripts/python.exe scripts/update_po.py   # 然后补 zh_CN 的 msgstr
.venv/Scripts/python.exe scripts/compile_mo.py

# 3. 提交并推送
git add -A && git commit -m "<type(scope): 摘要>"
git push origin main

# 4. 仅当 website/ 有变更时
.venv/Scripts/python.exe scripts/deploy_site.py

# 5. 仅当版本号有变更时
.venv/Scripts/python.exe scripts/release.py
```

**判断规则**

| 情况 | 必须做 |
| --- | --- |
| 任何代码/文档改动 | 门禁 → 提交 → 推送 |
| `website/` 下有文件变更 | 追加 `deploy_site.py`，并用 `curl` 校验线上与源码逐字节一致 |
| `src/novel_downloader/__init__.py` 的版本号变更 | 追加 `release.py`（会打 tag、构建三端、上传资产） |
| 仅本地实验、不希望上线 | 明确说明「未推送」，不要静默跳过 |

## 官网部署

- 站点目录：`website/`（纯静态，无构建步骤）
- 线上地址：<http://43.165.170.43/noveldl/>
- 服务器路径：`/var/www/noveldl`（root 所有，deploy 用户有免密 sudo）
- 认证：本机 `~/.ssh/id_ed25519` 已加入服务器的 `authorized_keys`，部署**无需密码**
- 部署脚本：`scripts/deploy_site.py`
  - 优先使用 SSH key/agent；仅在无 key 时才读 `NOVELDL_DEPLOY_PASSWORD`，且**绝不写入文件、日志或命令行**
  - `--dry-run` 只打印将要上传的文件
  - 上传到 `/tmp` 暂存并校验齐全后才 `sudo -n cp` 到线上，不会留下半个站点

## 发布

- 版本号在 `src/novel_downloader/__init__.py`；Android 侧同步 `android/app/build.gradle.kts` 的 `versionCode`/`versionName`
- `CHANGELOG.md` 顶部维护 `## Unreleased`，发版时改为 `## vX.Y.Z (YYYY-MM-DD)`
- `scripts/release.py` 幂等：已有 tag/release/资产按 SHA-256 跳过或替换，`--skip-build` 可断点续跑，`--dry-run` 只打印计划

## 硬性约束

- 提交信息用 conventional commits（`feat(scope):` / `fix(scope):` / `docs:` / `test:` / `build:`）
- 不提交任何凭据、token、密码、keystore
- 破坏性操作（改写历史、强推、删除 release）需先征得用户同意
- 新增用户可见字符串必须走 `t()` 并补 `zh_CN` 翻译，否则 `test_i18n_coverage` 会失败
