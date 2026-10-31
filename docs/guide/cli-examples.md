## CLI 使用示例

支持的站点列表详见: [站点支持文档](../supported-sites/index.md)

### 快速开始

```bash
# 根据 URL 下载书籍
novel-cli download https://www.hetushu.com/book/5763/index.html

# 指定站点下载 (例如 b520)
novel-cli download --site b520 8_8187

# 导出为 EPUB
novel-cli export --format epub 88888
```

---

### 登录/鉴权

当配置启用 `login_required: true` 时, CLI 会检测登录状态; 若未登录, 将提示在命令行输入当前站点的有效 Cookie 或账号信息。

部分站点的搜索/下载必须先登录 (例如番茄小说)。除了手工粘贴 Cookie 外, 也可以让下载器打开一个真实浏览器窗口完成登录, 自动抓取并保存会话 Cookie:

```bash
# 打开登录窗口, 登录成功后 Cookies 会保存到 <缓存目录>/<站点>/
novel-cli login fanqienovel

# 查看支持交互式登录的站点
novel-cli login --list
```

---

### 全局用法与帮助

```text
novel-cli [-h] [-v] [--lang LANG] COMMAND [ARGS]...

Options:
  -h, --help     显示此帮助信息并退出
  -v, --version  显示程序版本号并退出
  --lang LANG    覆盖本次运行使用的界面语言（例如 en、zh）。需放在子命令之前。
```

> 所有子命令均支持 `--help` 查看对应帮助文本。

---

### 子命令总览

```text
Commands:
  download    通过书籍 ID 或 URL 下载小说
  search      在一个或多个站点搜索书籍
  follow      追更书籍并检查新章节
  export      导出已下载的小说
  login       为站点打开登录窗口并保存其会话 Cookies
  config      管理应用配置和设置
  clean       清理应用日志、缓存与书籍数据
  storage     显示应用数据目录的磁盘占用
  doctor      检查运行环境并诊断常见问题
  sources     列出、校验、安装和移除规则书源
  music       搜索并下载来自多个音乐平台的歌曲
  completion  输出 novel-cli 的 shell 补全脚本
```

---

#### 1. download 子命令

下载完整小说, 支持三种方式:

1. **直接传入 URL**
2. **指定站点 + 书籍 ID**
3. **省略参数, 从配置文件读取 ID**

**Synopsis**

```bash
novel-cli download [-h] [--site SITE] [--config CONFIG] [--start START] [--end END] [--no-export] [--format FORMAT [FORMAT ...]] [--json] [book_ids ...]
```

**Options**

* `book_ids | url`:
    * 输入书籍 ID (配合 `--site` 使用)
    * 输入书籍/章节 URL (自动解析站点与书籍 ID)
    * 省略时将从配置文件读取 `book_ids`
* `--site SITE`: 站点键 (如 `qidian`, `b520`, ...), 若直接传入 URL, 可省略
* `--start START`: 起始章节**唯一 ID** (仅用于第一个 `book_id`)
* `--end`: 结束章节**唯一 ID**, **包含** (仅用于第一个 `book_id`)
* `--no-export`: 仅下载, 不进行导出。启用后将跳过导出步骤
* `--format FORMAT [FORMAT ...]`: 导出格式: `txt` / `epub` / `html`, 可指定多个, 默认使用配置文件设定
* `--json`: 以 JSON 输出到标准输出并退出 (结构见文末表格)

> `--start` / `--end` 用于临时下载部分章节, 仅影响**第一个**命令行提供的 `book_id`。
>
> `--start` 和 `--end` 接收**章节唯一 ID**, 并非 "第几章" 的序号。
>
> 需要复杂范围/忽略章节等, 请在配置文件使用结构化 `book_ids`; 参见 [配置文件说明](./settings-reference.md)。

**Examples**

![cli_download](../assets/images/cli_download.gif)

```bash
# 方式一: 直接通过 URL 启动下载
novel-cli download https://www.hetushu.com/book/5763/index.html

# 方式二: 指定站点 + 书籍 ID
novel-cli download --site b520 8_8187
novel-cli download --site n23qb 12282
novel-cli download --site qidian 1010868264

# 只下载部分章节
novel-cli download --site qidian 1010868264 --start 402902546 --end 406807540

# 仅下载, 跳过导出步骤
novel-cli download --no-export --site qidian 1010868264

# 方式三: 从配置文件中读取 ID
novel-cli download --site n23qb
```

---

#### 2. search 子命令

按关键字搜索小说

**Synopsis**

```bash
novel-cli search [-h] [--site SITE] [--config CONFIG] [--limit N] [--site-limit M] [--timeout SECS] [--format FORMAT [FORMAT ...]] [--json] [--first] [--sort {quality,priority,site,title,author,words,update}] keyword
```

**Options**

* `keyword`: 搜索关键字
* `--site SITE`, `-s SITE`: 指定搜索站点, 可多次使用以指定多个站点, 不指定则搜索全部支持站点
* `--limit N`, `-l N`: 总体结果上限 (最小 1), 默认为**不限制**
* `--site-limit M`: 单站点结果上限 (最小 1), 默认为 `10`
* `--timeout SECS`: 请求超时时间 (秒), 默认为 `5.0`
* `--format FORMAT [FORMAT ...]`: 结果确认后自动下载时的导出格式 (默认使用配置文件设定)
* `--json`: 将所有搜索结果以 JSON 输出到标准输出后退出 (便于脚本消费)
* `--first`: 直接下载排名第一的结果, 无需确认 (非交互模式)
* `--sort {quality,priority,site,title,author,words,update}`: 结果排序方式, 默认为 `quality` (优选)

**Examples**

![cli_search](../assets/images/cli_search_santi.gif)

```bash
# 搜索所有站点 (默认全部)
novel-cli search 三体

# 指定单个站点 (如 b520)
novel-cli search --site b520 三体

# 搜索 b520, 返回最多 5 条结果
novel-cli search --site b520 --limit 5 三体

# 总体返回最多 20 条, 每站点最多 5 条
novel-cli search --limit 20 --site-limit 5 三体

# 指定多个站点 (b520 和 n23qb)
novel-cli search -s b520 -s n23qb 三体
```

!!! note "浏览器辅助搜索"
    少数站点 (如番茄) 的搜索接口需要站点自身 JS 生成的签名, 纯 HTTP 请求拿不到结果, 因此**聚合搜索时会跳过这些站点** (不影响其它站点)。
    显式指定该站点时会启用浏览器辅助搜索: 复用登录会话启动一个隐藏浏览器窗口, 由站点自身 JS 发起已签名的搜索请求并截获结果 JSON。

    ```bash
    novel-cli login fanqienovel        # 先建立登录会话
    novel-cli search -s fanqienovel 斗破苍穹
    ```

    浏览器辅助搜索需要安装可选的浏览器组件; 若组件缺失, 命令会给出安装提示。

---

#### 3. export 子命令

导出已下载的小说为指定格式

**Synopsis**

```bash
novel-cli export [-h] [--format FORMAT [FORMAT ...]] [--site SITE] [--config CONFIG] [--start START] [--end END] [--stage STAGE] [--json] [book_ids ...]
```

**Options**

* `book_ids`: 要导出的一个或多个书籍 ID (可选; 若省略则会交互式选择)
* `--format FORMAT [FORMAT ...]`: 导出格式: `txt` / `epub` / `html`, 可指定多个, 默认使用配置文件设定
* `--site SITE`: 站点键 (如 `b520`, `qidian`, ...), (可选; 若省略则会交互式选择)
* `--config CONFIG`: 指定配置文件路径 (可选)
* `--start START`: 起始章节 ID (仅对第一本书生效)
* `--end END`: 结束章节 ID (仅对第一本书生效)
* `--stage STAGE`: 导出阶段 (例如 `raw`、`cleaner`), 默认使用最后阶段
* `--json`: 以 JSON 输出到标准输出并退出 (结构见文末表格)

**Examples**

![cli_search](../assets/images/cli_export.gif)

```bash
# 指定导出格式为 EPUB
novel-cli export --format epub

# 指定站点来源并导出多本书
novel-cli export --site b520 12345 23456

# 省略 site 和 book_ids, 进入交互模式选择站点和书籍
novel-cli export
```

> 提示: 未指定 `--site` 或 `book_id` 时, 将进入交互模式, 从已有下载的站点和书籍中进行选择。

---

#### 4. config 子命令

初始化和管理下载器设置, 包括切换语言等

**Synopsis**

```bash
novel-cli config COMMAND [ARGS]...
```

**Subcommands**

* `init [--force]`: 在当前目录初始化默认配置文件 (`./settings.toml`); `--force` 覆盖已存在文件
* `set-lang LANG`: 切换 CLI 语言 (`zh_CN` / `en_US`)

**Examples**

```bash
# 切换界面语言为英文
novel-cli config set-lang en_US

# 初始化默认配置到当前目录
novel-cli config init

# 强制覆盖已存在的配置文件
novel-cli config init --force
```

---

#### 5. clean 子命令

用于清理应用运行过程中产生的**内部状态 / 日志 / 站点缓存 / 书籍原始数据**。

清理后, 下次运行会自动重新生成对应资源 (如重新抓取 metadata / 章节 / 媒体等)。

`clean` 包含 4 个子命令:

* `state`: 清理内部状态文件 (运行时状态、临时信息)
* `logs`: 清理应用日志文件
* `cache`: 清理站点级缓存 (字体/共享资源等, 按站点目录划分)
* `book`: 清理书籍原始缓存数据 (章节 SQLite / metadata / media / 或整书目录)

**Synopsis**

```bash
novel-cli clean [-h] {state,logs,cache,book} ...
```

**Examples**

```bash
# 查看 clean 总帮助
novel-cli clean --help
```

---

#### 5.1 clean state 子命令

清理内部状态文件 (例如运行时记录、临时信息)

**Synopsis**

```bash
novel-cli clean state [-h] [-y]
```

**Options**

* `-y, --yes`: 跳过确认提示, 直接执行

**Examples**

```bash
# 清理内部状态
novel-cli clean state

# 强制清理 (跳过确认)
novel-cli clean state --yes
```

---

#### 5.2 clean logs 子命令

清理应用生成的日志文件。

**Synopsis**

```bash
novel-cli clean logs [-h] [--config CONFIG] [-y] [--dry-run]
```

**Options**

* `--config CONFIG`: 配置文件路径
* `-y, --yes`: 跳过确认提示
* `--dry-run`: 仅显示将被删除的日志文件, 不实际删除

**Examples**

```bash
# 试运行: 查看将要删除的日志文件
novel-cli clean logs --dry-run

# 清理日志
novel-cli clean logs

# 强制清理日志 (跳过确认)
novel-cli clean logs -y

# 指定配置文件位置
novel-cli clean logs --config ./settings.toml
```

---

#### 5.3 clean cache 子命令

清理站点级缓存目录, 缓存目录结构为:

```
<cache_dir>/<site_key>/
```

例如站点共享字体或解析缓存等。

**Synopsis**

```bash
novel-cli clean cache [-h] [--config CONFIG] [--site SITE] [-y]
```

**Options**

* `--config CONFIG`: 配置文件路径
* `--site SITE`: 站点键值 (如 `qidian`, `b520`), 省略时清理全部站点缓存
* `-y, --yes`: 跳过确认提示

**Examples**

```bash
# 清理指定站点缓存
novel-cli clean cache --site qidian

# 清理全部站点缓存 (删除整个 cache_dir)
novel-cli clean cache

# 跳过确认提示
novel-cli clean cache --site b520 -y

# 指定配置文件
novel-cli clean cache --config ./settings.toml
```

---

#### 5.4 clean book 子命令

清理一本或多本书的原始缓存数据, 数据目录结构为:

```
<raw_data_dir>/<site_key>/<book_id>/
```

默认会删除章节 SQLite 数据 (可按 `--start/--end` 限定范围)

可按需指定是否删除 media 或整书目录。

**Synopsis**

```bash
novel-cli clean book [-h]
    [--site SITE] [--config CONFIG]
    [--start START] [--end END]
    [--stage STAGE]
    [--no-chapters] [--metadata] [--media] [--all]
    [-y]
    [book_ids ...]
```

**Options**

* `book_ids`: 一个或多个书籍 ID
* `--site SITE`: 站点键值
* `--config CONFIG`: 配置文件路径
* `--start START`: 起始章节 ID (仅对第一本书生效)
* `--end END`: 结束章节 ID (仅对第一本书生效, **包含 end**)
* `--stage STAGE`: 清理阶段 (如 `raw`, `cleaner`), 默认 `raw`
* `--no-chapters`: 不删除章节数据 (SQLite 不动)
* `--metadata`: 删除 metadata JSON
* `--media`: 删除媒体资源目录 (图片等)
* `--all`: 删除该书**全部原始数据目录** (忽略其他清理选项), 若同时省略 `book_ids`, 则清理指定站点/全部站点的整站 raw_data
* `-y, --yes`: 跳过确认提示

**Examples**

```bash
# 默认清理
novel-cli clean book --site qidian 1010868264

# 清理指定范围章节 (仅第一本书生效)
novel-cli clean book --site qidian 1010868264 --start 402902546 --end 406807540

# 只清理 metadata, 不清章节
novel-cli clean book --site qidian 1010868264 --no-chapters --metadata

# 清理 media (图片等)
novel-cli clean book --site qidian 1010868264 --media

# 删除整本书所有原始数据 (直接删目录)
novel-cli clean book --site qidian 1010868264 --all

# 删除指定站点全部 raw_data (省略 book_ids 且 --all)
novel-cli clean book --site qidian --all

# 删除全部站点 raw_data (省略 site 且 --all)
novel-cli clean book --all

# 跳过确认提示
novel-cli clean book --site qidian 1010868264 --all -y
```

> 提示: `--start/--end` 接收**章节唯一 ID**, 并非 "第几章" 的序号

---

#### 6. login 子命令

为需要登录的站点打开一个真实浏览器窗口, 登录成功后自动保存会话 Cookie, 供后续搜索/下载复用。

需要安装可选的图形界面组件, 否则命令会提示安装。

**Synopsis**

```bash
novel-cli login [-h] [--timeout SECONDS] [--list] [site]
```

**Options**

* `site`: 站点键, 默认为 `fanqienovel`
* `--timeout SECONDS`: 等待登录完成的秒数, 默认 `300`
* `--list`: 列出支持交互式登录的站点

**Examples**

```bash
# 登录番茄小说并保存 Cookies
novel-cli login fanqienovel

# 自定义等待时间 (秒)
novel-cli login fanqienovel --timeout 600

# 查看支持交互式登录的站点
novel-cli login --list
```

> 保存位置: `<缓存目录>/<站点>/aiohttp.cookies`
>
> 登录窗口关闭且未检测到登录时命令以非零状态退出, 不会保存任何 Cookies。

---

#### 7. music 子命令

搜索并下载来自多个音乐平台的歌曲。该功能依赖可选组 `music`, 未安装时命令会打印安装提示:

```bash
pip install "novel-downloader[music]"
```

`music` 包含五个子命令: `platforms`、`search`、`download`、`export-m3u`、`health`。

**Synopsis**

```bash
novel-cli music [-h] {platforms,search,download,export-m3u,health} ...

novel-cli music platforms

novel-cli music search [-h] [--platform KEY] [--limit N] [--json] keyword

novel-cli music download [-h] [--keyword KEYWORD] [--platform KEY] [--limit N]
                         [--level {hires,lossless,exhigh,higher,standard}]
                         [--out DIR] [--pick N[,N|ID]] [--yes]
                         [KEYWORD]

novel-cli music export-m3u [-h] --keyword KEYWORD [--platform KEY] [--limit N]
                           [--out FILE]

novel-cli music health [-h] [--platform KEY] [--keyword KEYWORD]
```

**General Options**

* `--platform KEY`: 音乐平台标识, 默认为 `netease` (网易云音乐); 完整列表见 `novel-cli music platforms`
* `--limit N`, `-l N`: 最大结果数, 默认 `20`

**platforms**

列出可用的音乐平台及其说明:

```bash
novel-cli music platforms
```

**search**

按关键字搜索歌曲, 输出结果表格 (序号、标题、歌手、专辑、时长、是否 VIP):

```bash
novel-cli music search "关键词" --platform netease --limit 10
```

* `--json`: 以 JSON 输出搜索结果 (包含 `platform`、`keyword`、`total` 与 `tracks` 列表) 后退出, 便于脚本消费

```bash
novel-cli music search "关键词" --json
```

**download**

先搜索, 再选择并下载歌曲:

* 位置参数 `KEYWORD` 或 `--keyword KEYWORD`: 下载前搜索的关键词 (二选一)
* `--level`: 音质档位, 可选 `hires` / `lossless` / `exhigh` / `higher` / `standard`; 省略时取音乐设置, 通常为 `exhigh`
* `--out DIR`: 输出目录; 省略时取音乐设置 (未配置则使用默认下载目录)
* `--pick N[,N|ID]`: 无需确认, 按搜索结果序号 (从 1 开始) 或歌曲 ID 下载, 可传入多个值
* `--yes`: 无需确认, 直接下载全部搜索结果

未指定 `--pick` 或 `--yes` 且处于交互式终端时, 会进入分页选择界面 (输入编号、`a` 全选、`n` 下一页、`p` 上一页, 回车取消)。非交互式环境下必须显式提供 `--pick` 或 `--yes`。

**export-m3u**

把搜索结果导出为 M3U 歌单, 不下载音频:

* `--keyword KEYWORD, -k KEYWORD` (必填): 用于搜索待添加歌曲的关键词
* `--out FILE`: 歌单路径, 默认 `<输出目录>/<关键词>.m3u8`

```bash
novel-cli music export-m3u --keyword "关键词" --platform netease --limit 10
```

**health**

逐个平台执行一次受限搜索 (只取 1 条结果), 检查平台是否可正常连通; 用于排查网络或平台故障。搜索成功但结果为空也算连通 (如未配置凭据的鉴权型平台):

* `--platform KEY`: 只检查指定平台, 省略时检查全部平台
* `--keyword KEYWORD`: 连通性探测使用的关键词, 默认 `test`

```bash
# 检查全部平台
novel-cli music health

# 只检查网易云
novel-cli music health --platform netease
```

**Examples**

```bash
# 查看可用平台
novel-cli music platforms

# 在网易云音乐搜索
novel-cli music search "关键词"

# 在 QQ 音乐搜索并输出 JSON
novel-cli music search "关键词" --platform qq --json

# 下载第 1 条搜索结果
novel-cli music download "关键词" --pick 1

# 下载第 1、3 条结果, 指定高解析度音质与输出目录
novel-cli music download "关键词" --pick 1,3 --level hires --out ./music

# 下载全部搜索结果 (无需确认)
novel-cli music download "关键词" --yes

# 通过搜索结果中的歌曲 ID 下载
novel-cli music download "关键词" --pick 123456789
```

> 下载进度、标签写入、封面与歌词保存等行为遵循音乐设置 (如 `embed_tags`、`embed_cover`、`save_lyric`、`split_folder`、`skip_existing`)。

---

#### 8. follow 子命令

维护一份「追更列表」, 并检查已关注书籍是否发布了新章节。

追更记录保存在应用主目录的 `watchlist.json` 中 (即 `NOVEL_HOME`, 默认 `%LOCALAPPDATA%\novel_downloader\watchlist.json`), 与 `settings.toml` 中的 `book_ids` 相互独立。

**Synopsis**

```bash
novel-cli follow [-h] {add,remove,list,check} ...

novel-cli follow add [-h] [--title TEXT] site book_id
novel-cli follow remove [-h] site book_id
novel-cli follow list [-h] [--json]
novel-cli follow check [-h] [--all] [--download] [--wait]
                       [--wait-timeout SECS] [--timeout SECS]
```

**add / remove**

* `site`: 站点键 (如 `b520`, `qidian`)
* `book_id`: 书籍 ID
* `--title TEXT` (仅 `add`): 检查新章节时用于搜索的显示标题。省略时 `follow check` 只能拿 `book_id` 当关键字搜索, 命中率较低, 建议始终提供。

```bash
# 加入追更列表
novel-cli follow add b520 8_8187 --title "示例小说"

# 重复 add 会更新已有条目 (不会清掉已记录的最新章节)
novel-cli follow add b520 8_8187 --title "示例小说（修订名）"

# 移除
novel-cli follow remove b520 8_8187
```

**list**

```bash
# 表格输出: 站点 / 书籍 ID / 标题 / 最新章节 / 更新时间
novel-cli follow list

# 机器可读输出 (JSON 数组)
novel-cli follow list --json
```

`--json` 的每个元素包含 `site`、`book_id`、`title`、`last_chapter`、`updated_at`。

**check**

* `--all`: 同时显示已是最新的书籍和未找到结果的书籍; 省略时只报告新章节和错误。
* `--download`: 对发现新章节的书打印可直接执行的 `novel-cli download ...` 命令。
* `--wait`: 配合 `--download` 使用, 实际执行下载: 把有新章节的书在任务管理器中逐本排队下载, 并等待每本结束后再处理下一本。
* `--wait-timeout SECS`: 每本下载的等待上限 (秒), 默认 `1800`; 仅在 `--wait` 下生效。
* `--timeout SECS`: 每本书的搜索超时, 默认 `5.0` 秒。

```bash
# 只检查并报告新章节
novel-cli follow check

# 显示所有书籍的状态
novel-cli follow check --all

# 有新章节时, 额外打印下载命令
novel-cli follow check --download

# 有新章节时自动逐本下载并等待完成 (每本最多等默认的 1800 秒)
novel-cli follow check --download --wait

# 自定义每本下载的等待上限 (例如 1 小时)
novel-cli follow check --download --wait --wait-timeout 3600
```

`check` 默认只做搜索, **不会直接下载**: 下载是长任务, CLI 没有后台调度器, 因此它只打印命令, 保持命令快速、非阻塞、可预测。需要自动下载时有两种方式: 把打印出的 `novel-cli download --site <site> <book_id>` 交给计划任务 (见 [常见任务配方](./recipes.md)), 或改用 `follow check --download --wait`, 让 CLI 自己逐本下载并等待完成。

`--wait` 的语义:

* **逐本下载**: 有新章节的书按顺序逐本排队下载, 一本结束 (完成/失败/取消) 后才开始下一本, 不会并发施压站点。
* **超时计为取消**: `--wait-timeout` 限定单本的等待时间; 超时的任务会被取消, 在汇总中计为「已取消」, 其余书籍仍有各自完整的等待时间。
* **汇总输出**: 全部结束后打印 `Download summary: X completed, Y cancelled, Z failed.` 样式的汇总。
* **退出码**: 只有没有检查失败且所有下载都成功完成时才返回 `0`; 任意检查失败, 或任意一本下载失败/取消/超时, 都以 `1` 结束。
* `--wait` 必须与 `--download` 同用, 否则属于用法错误 (退出码 `2`)。

> 首次检查某本书时, 只会记录当前最新章节 (显示为 `First chapter recorded ...`), 不会误报为更新。
>
> 只要有任意一本书检查失败 (网络/插件异常), 命令最终以退出码 `1` 结束, 即使其它书检查成功。

---

#### 9. storage 子命令

统计应用四个数据目录的磁盘占用, 并可一次性清理:

| 类别 | 目录来源 |
| --- | --- |
| Raw data | `general.raw_data_dir` |
| Cache | `general.cache_dir` |
| Downloads | `general.output_dir` |
| Logs | `general.debug.log_dir` |

**Synopsis**

```bash
novel-cli storage [-h] [--json] [--clean] [-y]
```

**Options**

* `--json`: 以 JSON 输出统计结果到标准输出并退出。
* `--clean`: 打印表格后删除上述已存在的数据目录 (等同于批量清理 raw/cache/downloads/logs)。
* `-y, --yes`: 跳过 `--clean` 的确认提示。
* `--json` 与 `--clean` 不能同时使用 (退出码 `2`)。

**Examples**

```bash
# 查看占用
novel-cli storage

# 机器可读输出
novel-cli storage --json

# 清理全部数据目录 (会要求确认)
novel-cli storage --clean

# 跳过确认 (脚本/计划任务中使用)
novel-cli storage --clean -y
```

`--json` 的结构为 `{ "raw": {...}, "cache": {...}, "downloads": {...}, "logs": {...}, "total": {...} }`; 每个目录对象包含 `label`、`path`、`files`、`bytes`, `total` 只含 `files` 与 `bytes`。

---

#### 10. doctor 子命令

执行一组**离线**自检, 用于快速定位环境问题。

**Synopsis**

```bash
novel-cli doctor [-h] [--port N]
```

**检查项**

* Python 版本是否 >= 3.11
* 已安装包版本
* 是否找到 `settings.toml` / `settings.json`
* 配置能否解析为根表
* 输出目录能否创建并写入 (会创建该目录并写入/删除一个临时文件)
* 已注册下载站点数量
* 可加载的搜索器数量
* 可选音乐依赖 (`mutagen`、`qrcode`、`websocket`) — 缺失只提示, 不判定失败
* `127.0.0.1:<port>` 是否可绑定 (默认 8081)

**Options**

* `--port N`: 要检查可用性的 API 端口, 默认 `8081`。

**Examples**

```bash
# 默认检查
novel-cli doctor

# 检查自定义 API 端口
novel-cli doctor --port 9000
```

> 所有必需检查通过时退出码为 `0`, 任意必需检查失败为 `1`。失败项下方会打印一条可执行的修复建议。

---

#### 11. completion 子命令

输出 `novel-cli` 的 shell 补全脚本。补全内容由命令树自动生成, 是一份扁平词表 (子命令名 + 选项名, 不含选项值), 因此新增子命令后无需手动维护脚本。

**Synopsis**

```bash
novel-cli completion [-h] [--shell {bash,zsh,powershell}]
```

**Options**

* `--shell {bash,zsh,powershell}`: 生成补全脚本的目标 shell; 省略时自动检测: `$SHELL` 指向 bash/zsh 时跟随, 否则 Windows 默认 `powershell`, 其它平台默认 `bash`。

**Examples**

```bash
# Bash: 当前会话立即生效; 写入 ~/.bashrc 可持久化
source <(novel-cli completion --shell bash)

# Zsh: 脚本自带 #compdef 声明, 在已启用补全的交互会话中 source 即可
source <(novel-cli completion --shell zsh)

# Zsh (持久化): 放入 fpath 中的目录, 文件名必须为 _novel-cli
novel-cli completion --shell zsh > "${fpath[1]}/_novel-cli"
```

```powershell
# PowerShell: 追加到 $PROFILE, 每次启动自动注册补全
novel-cli completion --shell powershell >> $PROFILE
```

---

### 退出码与 `--json` 输出契约

#### 退出码

| 退出码 | 含义 | 典型场景 |
| --- | --- | --- |
| `0` | 成功 | 命令正常完成; 搜索无结果、追更无新章节也属于成功 |
| `1` | 运行时失败 | 网络/站点错误、登录失败、配置无法解析、下载或导出未完成、任意追更检查失败、追更 `--wait` 有下载未成功完成、`doctor` 必需项失败、`sources list --check` 探针失败、`storage --clean`/`clean` 删除失败 |
| `2` | 用法错误 | 缺少或冲突的命令行参数 (含 argparse 的参数错误)、`--json` 与 `--first` 同用、`--json` 与 `--clean` 同用、未提供 URL/书籍 ID、`config set-lang` 传入不支持的语言、`music` 缺少关键字或非交互环境下未给 `--pick`/`--yes` |

按 `Ctrl+C` 中断时, CLI 在 stderr 输出 `Aborted.` 并以 `1` 退出。

#### `--json`

带 `--json` 时:

* 结构化结果**只**写到 stdout; 日志、进度、错误和提示写到 stderr, 便于管道消费。
* 命令不会进入交互式选择; 需要的参数必须显式提供。
* 即使最终退出码非 `0`, 也可能已输出一份 JSON 摘要 (例如 `download --json` 在部分书籍失败时)。

| 命令 | 选项 | stdout 结构 |
| --- | --- | --- |
| `download` | `--json` | `{"site": ..., "books": [{"book_id", "downloaded", "exported"}]}`; `exported` 为 `{格式: [路径...]}`; 指定 `--no-export` 时为 `null` |
| `search` | `--json` | 搜索结果数组, 每项含 `site`、`book_id`、`book_url`、`cover_url`、`title`、`author`、`latest_chapter`、`update_date`、`word_count`、`priority` |
| `export` | `--json` | `{"site": ..., "books": [{"book_id", "exported"}]}` |
| `sources list` | `--json` | 数组, 每项含 `site_key`、`name`、`search`、`hosts`、`origin`; 加 `--check` 时 `status` 才有值 (否则为 `null`) |
| `music search` | `--json` | `{"platform", "keyword", "total", "tracks": [...]}` |
| `follow list` | `--json` | 数组, 每项含 `site`、`book_id`、`title`、`last_chapter`、`updated_at` |
| `storage` | `--json` | `{"raw", "cache", "downloads", "logs", "total"}` |

```bash
# 典型脚本用法: 取第一本书的 ID
novel-cli search --site b520 --json "三体" | jq -r '.[0].book_id'

# 检查追更列表并统计条目数
novel-cli follow list --json | jq 'length'

# 读取存储占用总字节数
novel-cli storage --json | jq '.total.bytes'
```

> `login --list` 也输出纯文本列表, 但不是 JSON; `sources check`、`config`、`clean` 目前没有 `--json`。
>
> 上面的 `jq` 示例需要另行安装 [jq](https://jqlang.github.io/jq/); 不安装时可直接查看原始 JSON 输出。

---

### 附录 A: 术语与约定

* **SITE (站点键)**: 在命令中用于指明站点的短名称 (如 `qidian`, `b520`, `n23qb`) 。
* **章节唯一 ID**: 站点侧用于标识章节的 ID, **不是**连续的 "第 N 章" 序号; 在 `--start`/`--end` 中应传入此 ID。
* **配置文件路径**: 若未显式传入 `--config`, CLI 会按「配置优先级」自动解析。
