# 发布流程（维护者）

面向维护者的一页式发布手册。仓库的硬性约定（什么时候必须跑什么）写在 `AGENTS.md`，
本文不重复那些条款，只补充**为什么这样做、具体怎么敲、失败之后怎么办**。

一次发布产出 8 个 GitHub Release 资产：三个 Windows 包、一个安装器、一个 APK，
外加 `SHA256SUMS.txt`、`manifest.json` 和更新清单 `latest.json`。
发布脚本 `scripts/release.py` 把这些串成一条幂等的流水线，任何一步失败都不会留下
"看起来完整"的公开 release。

---

## 0. 前置条件 {#prerequisites}

| 项 | 要求 | 缺了会怎样 |
| --- | --- | --- |
| 操作系统 | Windows（PyInstaller 不能交叉编译，安装器只能在 Windows 上打） | 构建直接失败 |
| 解释器 | `.venv/Scripts/python.exe`（Python 3.12） | 下面所有命令都按这个路径写 |
| Inno Setup 6 | `ISCC.exe` 在 `PATH` 或默认安装目录 | `desktop` 目标拒绝启动；可用 `--skip-installer` 只发便携包 |
| Android 签名 | `android/keystore.properties` + keystore 文件（均被 git 忽略） | `release.py` 跳过 APK 并打印警告；单独跑 `assembleRelease` 会直接失败 |
| GitHub 凭据 | `GITHUB_TOKEN` / `GH_TOKEN`，或 `git credential fill` 能返回 `github.com` 的密码 | 打 tag 之前中止 |
| 网络 | `api.github.com` 与 `uploads.github.com` 可达；`github.com:443` 可不通 | 见[网络受限](#network) |

文档与部署工具是可选依赖：

```bash
.venv/Scripts/python.exe -m pip install -e ".[docs,deploy]"
```

---

## 1. 版本号：一个源头，四处副本 {#version-sync}

版本号只在 `src/novel_downloader/__init__.py` 里定义一次，其余都是**手工维护的副本**，
每一处都有不同的消费者，所以漏掉任何一处都会以不同方式翻车：

| 位置 | 谁读它 | 是否被自动门禁覆盖 |
| --- | --- | --- |
| `src/novel_downloader/__init__.py` 的 `__version__` | 包本身、PyInstaller 版本资源、`doctor`、`latest.json` | 源头 |
| `android/app/build.gradle.kts` 的 `versionName` / `versionCode` | APK 自报版本、系统升级判定 | `release.py` 与 `check_versions.py` 比对 `versionName`；`tests/test_android_release_config.py` 校验 `versionCode` |
| `CHANGELOG.md` 顶部的 `## v<version> (YYYY-MM-DD)` | 发布说明正文 | `release.py`：必须是最顶部的版本小节，且正文非空 |
| `pyproject.toml` | setuptools 打包 | `check_versions.py`：必须 `dynamic = ["version"]`，不得再写一份字面量 |
| `website/index.html` 的 JSON-LD `softwareVersion` | 搜索引擎结构化数据 | `tests/test_website_static.py` 固定它等于 `__version__` |

发版时按顺序改：

1. `__version__ = "X.Y.Z"`。
2. `versionName = "X.Y.Z"`，并按下面的规则重算 `versionCode`。
3. 在 `CHANGELOG.md` 顶部加 `## vX.Y.Z (YYYY-MM-DD)`，正文就是发布说明。
   旧版本小节原样保留在下面；不要留 `## Unreleased` 在它上面，`release.py` 要求
   本次版本是**最顶部**的版本小节。
4. 同步 `website/index.html` 的 `softwareVersion`。

### 1.1 `versionCode` 是"第几个版本"，不是随手加一 {#version-code}

`versionCode` 的规则是：**`CHANGELOG.md` 里 `## vX.Y.Z` 小节中，版本号 ≤ 当前版本的数量**
（`v1.0.0` → `1`，`v1.37.0` → `51`）。所以正确顺序是"先写 CHANGELOG 小节，再取序号填进去"：

```bash
.venv/Scripts/python.exe -c "
import re,pathlib
text = pathlib.Path('CHANGELOG.md').read_text(encoding='utf-8')
versions = {tuple(map(int, m)) for m in re.findall(r'^## v(\d+)\.(\d+)\.(\d+)', text, re.M)}
current = (1, 38, 0)          # 改成这次要发的版本
print(sum(1 for v in versions if v <= current))
"
```

把它填进 `versionCode`。`tests/test_android_release_config.py` 会断言 `versionCode` 等于这个
序号，并检查历史序号没有重复；复用会让 Android 认为新包是旧包、直接忽略升级。
序号完全由 CHANGELOG 的版本小节数决定：漏写一个小节，序号就少一，所以"先写 CHANGELOG
再算序号"是唯一稳妥的顺序。

然后跑一次一致性检查，它会一次性告诉你哪几处还没对齐：

```bash
.venv/Scripts/python.exe scripts/check_versions.py
```

输出形如 `[OK  ] package/Android versionName: 1.37.0 (versionCode 51)`；任何一行 `FAIL`
都会指出该改哪个文件。CI 的 `versions` job 在每次 push 上跑同一个脚本。

!!! note "不受门禁覆盖的字面量"
    `docs/guide/settings-reference*.md` 里有一份桌面偏好导出的示例 JSON，其中的
    `"app_version"` 只是文档示例，没有测试固定它。发版后顺手同步即可，忘了不会失败。

---

## 2. 发布前门禁 {#pre-release-gates}

按下面顺序跑。这个顺序是"从便宜到昂贵"：语法与类型错误几秒钟就暴露，不必等到
PyInstaller 跑完十分钟。

```bash
# 1. 测试
.venv/Scripts/python.exe -m pytest tests -q

# 2. 静态检查
.venv/Scripts/python.exe -m ruff check src tests scripts
.venv/Scripts/python.exe -m ruff format --check src tests scripts
.venv/Scripts/python.exe -m mypy src

# 3. 版本一致性
.venv/Scripts/python.exe scripts/check_versions.py

# 4. 文档
.venv/Scripts/python.exe -m mkdocs build --strict
```

### 2.1 改动涉及用户可见字符串时

```bash
.venv/Scripts/python.exe scripts/update_pot.py
.venv/Scripts/python.exe scripts/update_po.py   # 然后手填 zh_CN 的 msgstr，删掉 #, fuzzy
.venv/Scripts/python.exe scripts/compile_mo.py
```

`messages.po` 入库，`*.mo` 与 `*.pot` 不入库（见 `.gitignore`）。
`tests/test_i18n_coverage.py` 要求每个 `t()` 的 msgid 都有非空、非 fuzzy 的 `zh_CN`
翻译，漏一条就红。

### 2.2 改动涉及 Android 时

```bash
cd android
./gradlew.bat testDebugUnitTest lintDebug --no-daemon --console=plain
```

本机没有 emulator/adb 时，这只能证明"编译、单测、lint 通过"，不能证明"手机上能用"。

Android 的 release 构建**没有 debug 签名回退**：没有 keystore 时 `assembleRelease` 会在任务
守卫处直接失败（`assembleDebug` 与单测不受影响）。这是刻意的——debug 签名的 APK 能装、
会报 release 的 `versionName`，之后真钥匙再也覆盖不了它（Android 拒绝签名变更），等于把
缺文件变成一条断掉的更新通道。`tests/test_android_release_config.py` 锁定了这条不变量，
以及 `isDebuggable = false`、`isShrinkResources = false` 等 release 配置。

### 2.3 每条门禁失败怎么办

| 门禁 | 常见原因 | 处理 |
| --- | --- | --- |
| `pytest` | 行为改动没同步测试；i18n 覆盖率不足 | 补测试或补 `zh_CN` 翻译，不要改断言迁就实现 |
| `ruff check` / `format --check` | 未格式化 | `ruff format src tests scripts` 后再跑一次 check |
| `mypy src` | 新增未注解函数、`Any` 泄漏 | 补注解；`scripts/` 下只有构建链与 `check_versions.py` 等被 CI 类型检查 |
| `check_versions.py` | 版本号只改了一处 | 按输出的文件名补齐 |
| `mkdocs build --strict` | 内部链接指向不存在的页面/锚点 | 修链接；新增页面要同时加进 `mkdocs.yml` 的 `nav` |
| `test_i18n_coverage` | 新字符串没翻译或有 `#, fuzzy` | 跑 2.1 的三步脚本 |

---

## 3. 执行发布 {#run-release}

### 3.1 先空跑

```bash
.venv/Scripts/python.exe scripts/release.py --dry-run
```

`--dry-run` **不执行任何子进程、不联网**：它只校验版本号、tag 形状、`CHANGELOG` 小节，
然后打印计划（构建命令、tag、资产清单、`latest.json` 会宣传哪些平台）。用它确认：

- `Tag behind` 一行是否符合预期（默认拒绝）；
- `Assets` 列表里有没有 `NovelDownloader-setup-<ver>-x64.exe`；
- `latest.json` 的平台数与键名（默认 5 个）。

### 3.2 正式发布

```bash
.venv/Scripts/python.exe -u scripts/release.py > build/rel.log 2>&1
tail -30 build/rel.log
```

!!! warning "不要把 release.py 接到管道后面"
    `release.py | tail` 的退出码来自 `tail`，会把失败吞成 0。必须重定向到文件再查看。
    `-u` 让日志实时落盘，进程被杀时也能看到最后一步。

阶段与失败位置：

| 阶段 | 做什么 | 失败时 |
| --- | --- | --- |
| 预检 | 版本/CHANGELOG/tag 形状/Android `versionName` | 任何一处不一致，**构建前**退出 2 |
| 凭据扫描 | `scan_secrets.py` 扫 Git 跟踪文件 | 命中即中止，只打印 `path:line: 规则` |
| 工作区 | `git status --porcelain` 必须干净 | 未提交改动会让发布中止（`--allow-dirty` 可跳过） |
| 构建 | `build_release.py` 三端 + `build_desktop.py` 安装器 | 退出 2；日志末尾 8 行会脱敏后打印 |
| 目录校验 | `verify_release_dir`：`manifest.json` 完整、无多余文件 | 见[断点续跑](#resume) |
| Android | `assembleRelease` + 16 KB 对齐门禁 | 对齐失败即中止；无签名材料时 `release.py` 警告并跳过 APK |
| 资产 | 收集、`verify_release_requirements`、重建 `latest.json`、内容审计 | 见[三道硬门禁](#hard-gates) |
| 打 tag / 推送 | 本地 tag、`git push`，必要时走 REST | 见[网络受限](#network) |
| Release | 建草稿 → 上传 → 核对 digest → 发布 | 失败留下可续跑的草稿 |

退出码只有两种：`0` 成功；`2` 是 `ReleaseError` 或参数错误。

---

## 4. 三道硬门禁 {#hard-gates}

这三项都在 `release.py` 里，失败即中止，且都发生在**打 tag 之前**——因为一旦 tag 推上去，
GitHub 上的引用就指向了这次构建，事后修正只能移动或删除 tag。

### 4.1 16 KB 页对齐（`scripts/check_android_alignment.py`）

Android 15 起，部分 arm64 设备使用 16 KB 内存页。这样的设备上，4 KB 对齐的 `.so`
**装得上、打不开**。两件事必须同时成立，且互相独立：

1. zip 内被 mmap 的条目（`lib/*/*.so` 与未压缩资源）偏移必须是 16 KB 的倍数；
2. 每个 `lib/*/lib*.so` 的 `PT_LOAD` 段 `p_align` 必须 ≥ 16 KB。

只查一项是假的安全感。`release.py` 对每个产出的 APK 都跑一遍，任一失败即中止。
单独复查某个 APK：

```bash
.venv/Scripts/python.exe scripts/check_android_alignment.py \
  android/app/build/outputs/apk/release/app-release.apk
```

输出会逐个列出 `zip_aligned` 与 `elf_p_align`，退出码 `0` 合格、`1` 违规、`2` APK 不可读。
修法是让依赖用 `-Wl,-z,max-page-size=16384` 构建，或升级那个依赖；不要绕过检查。

### 4.2 资产泄漏审计（`scripts/audit_release_assets.py`）

构建前的 `scan_secrets.py` 看的是**源码**；这一道看的是**真正要上传的字节**。
PyInstaller 可能把构建目录里的 `.env`、`.pdb` 或带 token 的样例配置打进包里，
除了这道门禁没有别的东西会检查。

它做三类检查：

- **发布面**：文件名必须在允许清单里、非空、大小合理；给了 `--release-json` 时还要与远端资产集一致。
- **归档成员**：ZIP/APK 成员不得包含穿越路径、VCS/构建目录、凭据、source map、调试符号。
- **原始字节**：按 1 MiB 分块（含 96 字节跨块重叠）扫描私钥头、部署密码、token 形状字符串。

命中只报 `资产: 成员:行号: 规则` 或标记名，**不回显明文**，所以可以直接贴进 issue。
这一道**没有跳过开关**。手动复查：

```bash
.venv/Scripts/python.exe scripts/audit_release_assets.py \
  --dir dist/release/novel-downloader-1.37.0-windows-x64
```

退出码 `0` 干净、`1` 有问题、`2` 参数或 I/O 错误。边界：单成员超过 2 MiB 跳过，
单资产文本扫描上限 64 MiB，整次审计上限 256 MiB；嵌套归档从不解压。

### 4.3 发布后校验（`verify_assets`）

上传是唯一一步"失败曾经不可见"的操作：并发运行或半途断网会留下一个看起来完整、
实则少文件的 release。所以 `release.py` 上传完会**重新读取 release**，逐个确认：

- 每个预期资产都存在；
- 远端 digest 与本地 SHA-256 一致（远端没有 digest 时按"必须替换"处理）；
- 存在 `latest.json` 时，它**必须**有远端 digest，否则拒绝把清单留在未校验状态。

release 全程以**草稿**创建，只有这一步通过才 `PATCH` 成公开。中途失败留下的是可续跑的
草稿，而不是半空的公开 release。

### 4.4 另外两道"够硬"的前置条件

- **构建前凭据扫描**：`--skip-secret-scan` 可以关掉，但只应在确认是误报时使用；
  关掉后整个构建都没有凭据检查。
- **桌面载荷契约**：`desktop` 目标默认必须收齐 `NovelDownloader-setup-<ver>-x64.exe`，
  否则拒绝发布——因为安装版用户只从 `windows-x64-installer` 更新，缺了它会静默回退到
  便携包，看起来发布成功，实际安装版用户收不到更新。用 `--skip-installer` 时清单里
  不会出现该平台，此时**便携 zip 必须存在**作为回退。

---

## 5. 断点续跑：`--skip-build` 与 `--allow-tag-behind` {#resume}

构建成功但上传失败（典型是 408 或断网）时，不要重新构建，直接复用已有的产物目录：

```bash
.venv/Scripts/python.exe -u scripts/release.py --skip-build > build/rel.log 2>&1
```

`--skip-build` 会复用 `dist/release/novel-downloader-<version>-*/`，并在做任何事之前
用 `verify_release_dir` 证明这个目录是完整的：

- `manifest.json` 存在，版本匹配，`failures` 为空；
- 每个请求的 target 都至少有一个产物且文件存在、非空、大小与记录一致；
- 目录里**没有** `manifest.json` 之外的多余文件（另一个版本残留的 zip 会被当成这次发布上传）。

已上传且 digest 一致的资产会按 SHA-256 跳过；digest 不同则删除重传。

**tag 落后 HEAD**（发版后又提交了文档之类）时，默认仍然拒绝，因为真实构建的产物来自
`HEAD`，不能挂到旧 tag 上。若确认现有产物就是 tag 那次构建的，用：

```bash
.venv/Scripts/python.exe -u scripts/release.py \
  --skip-build --allow-tag-behind > build/rel.log 2>&1
```

规则：

| 情况 | 结果 |
| --- | --- |
| `--allow-tag-behind` 不带 `--skip-build` | 参数组合直接拒绝（真实构建必须来自 HEAD） |
| tag 是 HEAD 的祖先 | 允许，打印提示，**tag 不移动** |
| tag 与 HEAD 分叉（不是祖先） | 拒绝，`--allow-tag-behind` 也救不了；需手工处理 tag |
| tag 指向别的提交 | 拒绝，绝不静默复用 |
| 远端已有 tag 但指向不同提交 | 拒绝 |

`release.py` **从不移动或删除 tag**。需要改 tag 属于破坏性操作，必须先征得同意。

---

## 6. 网络受限 {#network}

国内环境经常出现 **`github.com:443` 不通、`api.github.com` 正常**。两者是不同的服务：
构建、上传、创建 release 都走 API，只有 `git push` 走 github.com。

### 6.1 tag 推送的自动回退

`git push` 失败时，`release.py` 会用 `POST /repos/<slug>/git/refs` 直接创建
`refs/tags/<tag>`，指向 **tag 自身的提交**（`git rev-parse <tag>^{commit}`），不是 HEAD。
这样"发版后又提交了文档"不会把 tag 指错。

如果 REST 返回 422 且消息是 `Object does not exist`，含义是 **tag 指向的提交还没推到远端**。
先把 `main` 推上去，再重跑 `--skip-build`：

```bash
.venv/Scripts/python.exe scripts/push_via_api.py --dry-run
.venv/Scripts/python.exe scripts/push_via_api.py
```

`scripts/push_via_api.py` 只使用 Git Data API：读取远端分支当前提交，按 tree 找出
"内容已同步"的边界，把边界之后的提交逐条 replay（上传缺失 blob、重建 tree、保留原始
message/author/committer 与日期），最后**非 force** 更新分支 ref。远端若有本地不包含的
新提交，脚本直接报错退出，绝不覆盖。凭据同样只放 HTTP 头，不打印、不落盘。

注意：API 创建的提交 sha 与本地不同、**tree 一致**，所以本地 `main` 会与远端分叉。
之后继续走 API 推送，不要再普通 `git push`（会 non-fast-forward）。

### 6.2 一次性补救脚本

仓库里还有一个**未入库**的一次性脚本 `build/push_replay.py`（`build/` 被 git 忽略）。
它做同样的事，但把仓库和分支写死在文件里，并额外做一次内容级边界判定与推送后校验
（远端最终 tree 必须与本地 `HEAD` 逐文件一致）。它没有 `--dry-run`，也不接受参数，
只适合在确认远端内容确实等于某个本地提交时手工运行一次：

```bash
.venv/Scripts/python.exe build/push_replay.py
```

日常发布请用 `scripts/push_via_api.py`；后者有 `--dry-run`、参数化仓库与分支，并被测试覆盖。

### 6.3 大文件上传

APK 有 70 MB 量级，走系统代理常在半路断连。`upload_asset` 的行为是：

- 第 1 次尝试走系统代理；
- 之后 3 次改为**直连**（`ProxyHandler({})`），退避 4 / 8 / 16 秒；
- 单次上传超时 900 秒；读取类请求超时 120 秒、重试 3 次；
- 超时的那次可能其实已经传完，GitHub 会回 `already_exists`；脚本按名字查回资产并比对
  digest，一致就视为成功。

所以遇到 `HTTP 408 Upload body timed out`，直接：

```bash
.venv/Scripts/python.exe -u scripts/release.py --skip-build > build/rel.log 2>&1
```

代理与非代理都断的极端情况：用 `requests` 直连补传（`trust_env=False` + `proxies={}`），
补完再跑一遍 `--skip-build`，让 `verify_assets` 重新核对 digest。

!!! danger "不要手工往 release 目录里塞文件"
    `verify_release_dir` 会把不在 `manifest.json` 里的文件判为错误并中止。
    缺资产就重新构建，或者补齐构建产物后重跑，而不是往目录里拷一个文件。

---

## 7. 官网部署 {#website}

只有 `website/` 下有改动时才需要这一步；它与 release 是两条独立流程。

```bash
# 先看会传哪些文件，不连服务器
.venv/Scripts/python.exe scripts/deploy_site.py --dry-run

# 实际部署
.venv/Scripts/python.exe scripts/deploy_site.py
```

`SITE_FILES` 是显式允许清单，`website/` 下新增文件必须同时加进去，否则永远不会被发布。
脚本先上传到服务器 `/tmp` 暂存，校验齐全后才用两次 rename 原子替换
`/var/www/noveldl`；替换失败会恢复旧目录。

部署后逐字节核对（`curl` 与 `cmp` 都是 bash 侧路径，`/tmp` 可以直接用）：

```bash
curl -s http://43.165.170.43/noveldl/index.html -o /tmp/live-index.html
cmp /tmp/live-index.html website/index.html && echo "index OK"

curl -s http://43.165.170.43/noveldl/__no_such_page__ -o /tmp/live-404.html
cmp /tmp/live-404.html website/404.html && echo "404 OK"

curl -s http://43.165.170.43/noveldl/sitemap.xml -o /tmp/live-sitemap.xml
cmp /tmp/live-sitemap.xml website/sitemap.xml && echo "sitemap OK"
```

只做 `curl -I` 只能看到状态码，无法证明 body 是自定义页。404 与 nginx 配置的细节见
[官网静态站部署](website-deploy.md)。

---

## 8. 更新清单签名 {#signing}

签名机制已实现但**默认未启用**（`updater.TRUSTED_UPDATE_KEYS` 为空）。这里只给指针，
不重复整篇：

- **格式、canonical JSON、`key_id`、各端差异**：见[更新清单签名](update-signing.md)。
- **启用**：`build_update_manifest.py --keygen` 生成密钥 → 把公钥填进 Python 的
  `TRUSTED_UPDATE_KEYS` **和** 安卓的 `UpdateChecker.TRUSTED_UPDATE_KEYS`（两张表要分别
  构建发布）→ 之后每次发布都带 `--sign-key`。
- **换钥**：必须走"过渡版本同时信任新旧两把 → 用新钥签名 → 移除旧钥"三阶段，不能一步到位。
  细节见[更新清单签名](update-signing.md)第 4 节「滚动发布与换钥」。

启用后有一条硬约束：**每次发布都必须带 `latest.json`**，否则已固定公钥的客户端会拒绝更新
且不再回退 API。`release.py` 本来就把清单放在**最后**上传，确保它指向的文件都已就位。

发布时签名：

```bash
.venv/Scripts/python.exe scripts/release.py --sign-key ~/.novel_downloader/update.key
```

成功会打印 `==> latest.json signed (key id <id>)`；`--dry-run` 会提前告诉你这次会不会签名。
签名失败（读不到、不是十六进制、不是 32 字节）会在**打 tag 之前**中止。

---

## 9. 发布后校验 {#post-release}

`release.py` 自己已经核对了 digest，但一次独立复核能区分"脚本说成功"和"线上真的对"。

### 9.1 资产齐全

完整发布应有 **8 个**资产：

| 资产 | 来源 |
| --- | --- |
| `novel-cli-<ver>-windows-x64.zip` | `build_release.py` |
| `novel-api-<ver>-windows-x64.zip` | `build_release.py` |
| `NovelDownloader-<ver>-windows-x64.zip` | `build_release.py`（便携版） |
| `NovelDownloader-setup-<ver>-x64.exe` | `build_desktop.py`（Inno Setup） |
| `novel-downloader-<ver>-android.apk` | `gradlew assembleRelease` |
| `SHA256SUMS.txt` | 合并三端 + APK + 安装器 + `latest.json` 的摘要（当前 6 行） |
| `manifest.json` | 构建记录（targets / artifacts / failures） |
| `latest.json` | 更新清单，从实际资产重建 |

少一个的常见原因：`--skip-android`（7 个）、`--skip-installer`（7 个，且清单无
`windows-x64-installer`）、缺少 Android 签名材料（7 个 + 警告）。

```bash
V=1.37.0
curl -s "https://api.github.com/repos/lingyi-ling/novel-downloader/releases/tags/v$V" \
  | .venv/Scripts/python.exe -c "import json,sys; d=json.load(sys.stdin); print('draft', d['draft'], 'assets', len(d['assets'])); [print(' ', a['name'], a['size']) for a in d['assets']]"
```

未认证的 API 每小时只有 60 次配额，复核时省着用。

### 9.2 稳定端点与清单内容

客户端读的是 `releases/latest/download/latest.json`（302 到 CDN）。它必须与本地构建出的
那份**逐字节一致**：

```bash
mkdir -p build/relcheck
curl -sL -o build/relcheck/latest.json \
  https://github.com/lingyi-ling/novel-downloader/releases/latest/download/latest.json
cmp build/relcheck/latest.json "dist/release/novel-downloader-$V-windows-x64/latest.json" \
  && echo "manifest byte-identical"
```

再确认清单自我描述正确、平台齐全：

```bash
.venv/Scripts/python.exe -c "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8')); print(d['version'], d['tag'], d['repo']); print(sorted(d['platforms']))" build/relcheck/latest.json
```

预期 `version == tag[1:]`、`repo == lingyi-ling/novel-downloader`，平台是
`android`、`windows-x64-api`、`windows-x64-cli`、`windows-x64-installer`、
`windows-x64-portable` 五个（`--skip-installer` 时少最后一个）。

`SHA256SUMS.txt` 与 `latest.json` 对同一批文件给出相同摘要，两者不一致是发布阻断级 bug；
`release.py` 用 `validate_sums_against_manifest` 强制这一点，复核时可以抽查一行：

```bash
curl -sL "https://github.com/lingyi-ling/novel-downloader/releases/download/v$V/SHA256SUMS.txt" \
  | grep "NovelDownloader-setup-$V-x64.exe"
```

### 9.3 客户端视角

```bash
.venv/Scripts/novel-cli.exe update --check --json
```

它读同一份清单。启用签名后，还可以用公钥离线复核（不接触私钥）：

```bash
.venv/Scripts/python.exe scripts/build_update_manifest.py \
  --release-json payload.json --version "$V" --tag "v$V" \
  --repo lingyi-ling/novel-downloader \
  --check build/relcheck/latest.json --public-key <64 位十六进制公钥>
```

`payload.json` 是 `releases/tags/v$V` 的 API 响应。退出码 `0` 表示一致且签名有效；
不一致会打印 `stale: <字段>`。

---

## 10. 失败速查 {#troubleshooting}

| 现象 | 多半是 | 处理 |
| --- | --- | --- |
| `--version X does not match __version__ Y` | 只改了 `--version` 或只改了包 | 改 `__init__.py`，两者必须相等 |
| `CHANGELOG.md has no section ## vX.Y.Z (` | 忘了写小节，或写成了 `## X.Y.Z`（无 `v`） | 用 `## vX.Y.Z (YYYY-MM-DD)`，放在最顶部 |
| `CHANGELOG.md's newest version section is ...` | 本次版本不是最顶部 | 把本次小节移到最上面 |
| `android versionName '...' does not match` | Gradle 副本没同步 | 改 `build.gradle.kts` 的 `versionName` |
| `versionCode is N but X.Y.Z is release #M` | `versionCode` 不是 CHANGELOG 序号 | 按 [1.1](#version-code) 重算并填入 |
| `working tree has N uncommitted change(s)` | 有未提交改动 | 提交，或确认后用 `--allow-dirty` |
| `no GitHub token available` | 环境变量与 credential helper 都没有 | 设 `GITHUB_TOKEN`，或让 `git credential fill` 能返回密码 |
| `the desktop target requires the Inno Setup installer` | 没装 ISCC | 装 Inno Setup 6，或 `--skip-installer` 只发便携包 |
| `... is not 16 KB page-size ready` | 某个 `.so` 仍是 4 KB 对齐 | 用 `-Wl,-z,max-page-size=16384` 重建该依赖 |
| `release asset audit failed (...)` | 产物里有凭据/调试符号/异常路径 | 看报告的资产与成员，修构建输入；不要加跳过开关（没有） |
| `tag vX.Y.Z exists locally at ... but HEAD is ...` | tag 落后或分叉 | 落后用 `--skip-build --allow-tag-behind`；分叉只能手工处理 tag |
| `--allow-tag-behind requires --skip-build` | 参数组合不对 | 加上 `--skip-build`，或从 HEAD 重建 |
| `tag ... already exists on origin at ...` | 远端 tag 指向别的提交 | 停止，确认后再处理；`release.py` 不会移动 tag |
| REST 422 `Object does not exist` | tag 指向的提交还没推上远端 | 先 `push_via_api.py` 推 `main`，再 `--skip-build` |
| `HTTP 408 Upload body timed out` | 大资产走代理断流 | 直接 `--skip-build` 续跑，已传资产按 digest 跳过 |
| `latest.json` 线上 404 | release 仍是草稿、上传失败或被删 | 重跑 `--skip-build`；不要手工删清单 |
| 客户端报 `manifest-unavailable` | 清单取不到（草稿/网络/重定向到 HTTP） | 确认 release 已发布且资产存在 |
| `warning: the release has extra asset(s)` | 远端有本次构建之外的旧文件 | 无害（清单不指向它），需要整洁就手工删 |

---

## 11. 与 CI 的分工 {#ci}

`.github/workflows/release.yml` **只支持手动 dispatch**，故意不监听 tag push：本地发布脚本
自己推 tag，若 CI 同时在 tag 上启动，会再构建一套（没有签名 APK、没有安装器）并覆盖刚上传
的资产——v1.4.0 就是这么坏的。该 workflow 跑在 `windows-latest`，用 `GITHUB_TOKEN` 发布，
Android 因为没有签名材料会被跳过。

因此：

- 常规发布在本机跑 `release.py`（有 keystore、有 ISCC）。
- CI 的 `ci.yml` 只做门禁（lint / mypy / 版本一致性 / 测试 / Android 单测），不发布。
- 不要在 `release.yml` 上加 tag 触发器。

---

相关页面：[更新与自动更新](updates.md)（客户端行为与清单格式）、
[更新清单签名](update-signing.md)（签名与换钥）、
[官网静态站部署](website-deploy.md)（部署与 404 配置）、
[安全说明 § 发布资产内容扫描](security.md#release-asset-scan)。
