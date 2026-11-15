## `settings.toml` 配置说明

桌面端的「设置」页覆盖这里的常用项（输出目录、代理、自动更新、主题与语言），保存后写入 `settings.toml`：

![桌面端设置页](../assets/images/desktop_settings.png)

### 桌面设置页：搜索缓存 (Search cache)

桌面端「设置」页的「搜索缓存」卡片显示**当前桌面进程**的搜索结果缓存。搜索结果按查询参数缓存一小段时间，重复同一查询时直接复用，不再逐个访问站点；卡片读取的是只读快照：刷新不会清理过期条目、不会打开磁盘文件，也不会改变任何缓存行为。打开设置页时读取一次，清除成功后再读取一次。

| 卡片行 | 含义 |
| ------ | ---- |
| 内存条目 | 进程内保存的缓存条目数；包含 TTL 已过、但尚未被后续写入/读取顺带清理的条目 |
| 磁盘条目 | 缓存目录中本格式 payload 文件数，不区分是否已过期；未启用磁盘层时恒为 `0` |
| 缓存命中 | 进程启动以来的命中次数；内存命中与从磁盘恢复并提升到内存的条目都计入 |
| 过期条目 | 内存条目中已超过 TTL（默认 300 秒）的部分；只计数，读取卡片不会清除它们，磁盘层的过期文件也不计入此项 |

未取到值（缓存尚未使用）或读取失败时，数值显示为 `0`，行下方会给出错误说明；这不会影响正在进行的搜索。计数是桌面进程生命周期内的累计值，清空缓存不会重置。未命中（`misses`）与因内存容量上限（默认 64 条）发生的 LRU 淘汰（`evictions`）同样在累计，但本卡片不单独显示，完整字段见 [`novel-cli doctor`](./cli-examples.md#doctor-subcommand) 的「搜索缓存诊断」。磁盘层默认关闭，仅在搜索模块首次导入前设置 `NOVELDL_SEARCH_CACHE_DIR` 时启用；启用后每次写入后目录最多保留 256 个 payload，单个超过 2 MiB 的快照只留在内存。

**清除搜索缓存**

点击「清除搜索缓存」需要二次确认：第一次点击只把按钮文字改为「再次点击以清除」，并在 4 秒内保持待确认；4 秒内再次点击才真正清空，超时未点击会自动恢复为普通状态，不会一直保持待确认。执行期间按钮禁用。成功后：

* 内存中的全部条目被丢弃；
* 磁盘层启用时，缓存目录中本格式（`v1-*.json`）的 payload 文件会被删除（尽力而为：个别文件删不掉时会被忽略，不会因此报错）；若 `NOVELDL_SEARCH_CACHE_DIR` 指向与其他进程共用的目录，这些进程写入的磁盘缓存也会一并失效，其后的相同搜索只能依赖各自的内存条目，否则会重新访问站点；
* 目录中的其他文件和本缓存格式之外的条目不受影响；
* 累计的命中/未命中/淘汰计数保留。

成功时 Toast 提示「搜索缓存已清除」并刷新卡片；失败时 Toast 显示错误文本，卡片保留原值，可直接重试。

### 桌面设置导出 / 导入 (Settings export / import)

「设置 → Backup」卡片把**桌面偏好**导出为 JSON，并可在另一台机器上导入。导出的不是 `settings.toml`，而是桌面偏好文件 `desktop_prefs.json`（默认位于应用数据目录的 `music/` 下）；文件只包含可跨机器迁移的偏好项，不含任何凭据。导入是**合并写入**，不是恢复出厂设置。

**导出文件契约**

```json
{
  "app": "novel-downloader",
  "kind": "desktop-preferences",
  "format_version": 1,
  "app_version": "1.36.0",
  "exported_at": "2026-11-15T00:00:00Z",
  "settings": {
    "theme": "dark",
    "auto_check_updates": true,
    "auto_download_updates": true,
    "auto_install_on_exit": true,
    "search_filters": {
      "min_words": 10,
      "max_words": 500,
      "updated_after": "2024-01-02"
    },
    "search_sort": {"sort": "quality", "order": "desc"}
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `app` | 固定为 `novel-downloader`；值不符时导入直接拒绝 |
| `kind` | 固定为 `desktop-preferences`，用于把本文件与普通 JSON 区分开 |
| `format_version` | 当前为 `1`。只接受整数 `1`；`true`、`2`、`"1"` 等一律拒绝，不做向下兼容 |
| `app_version` | 导出时的应用版本，仅作参考；导入不与当前版本比对 |
| `exported_at` | UTC 时间戳（秒级，`Z` 结尾），仅作参考 |
| `settings` | 偏好负载，键值见下面的白名单 |

**白名单（只导出这些键）**

| `settings` 键 | 类型 | 含义 |
| --- | --- | --- |
| `theme` | `"light"` / `"dark"` | 外观主题 |
| `auto_check_updates` | bool | 自动检查更新 |
| `auto_download_updates` | bool | 后台下载更新 |
| `auto_install_on_exit` | bool | 退出时安装已下载的更新 |
| `search_filters` | object | 搜索过滤，可含 `min_words` / `max_words` / `updated_after`（`YYYY-MM-DD`）；未设置时为 `{}` |
| `search_sort` | object | 搜索排序，固定含 `sort`（`quality` / `priority` / `words` / `update`）与 `order`（`asc` / `desc`） |

**明确不导出**：

| 不导出的内容 | 原因 |
| --- | --- |
| 代理地址、代理用户名 / 密码 | 凭据不能随文件传播；地址也可能与本机网络绑定 |
| 音乐账号 token（TIDAL / Apple Music / SoundCloud 等） | 账号凭据，属于设备私密数据 |
| 输出目录、界面语言 | 存在 `settings.toml` / 独立状态文件中，按设备与个人习惯设置，不属于桌面偏好文件 |
| `last_update_check` 等节流状态 | 属于本机运行时状态，迁移没有意义 |
| 音乐下载选项（如 `embed_lyric`） | 与 CLI 共用音乐设置文件，导出会产生第二个事实来源 |

因此导出的 JSON 可以安全地放进同步盘或聊天工具；代价是换机后代理、账号、输出目录与语言需要重新设置（见「换机迁移的局限」）。

**导出行为**

* 路径留空时，写到桌面偏好文件同目录，文件名形如 `desktop-settings-20261115-143000.json`；指定路径没有扩展名时自动补 `.json`。
* 写入使用临时文件 + 原子替换，不会留下半个文件。
* 目标路径是已存在的目录时拒绝导出；其他写入失败只在卡片状态行和 Toast 中报错。
* 偏好文件缺失或不可读时，导出的是默认值（`theme=light`、三个更新开关均为开、过滤器为空），不会因此报错。

**导入校验**

导入在**写任何内容之前**完成全部校验；任一项失败都不会改动现有配置，也不会生成 `.bak`：

1. 路径不能为空；文件必须存在且是普通文件（目录会被拒绝）。
2. 文件不能超过 1 MiB（`1,048,576` 字节）；超过即拒绝，避免误选大文件或内存消耗。
3. 按 UTF-8 读取并允许 BOM（`utf-8-sig`）；其他编码或非法字节按「不是合法 JSON」拒绝。
4. 顶层必须是 JSON 对象；`app`、`kind` 必须精确匹配；`format_version` 必须严格等于整数 `1`（布尔值 `true` 也会被拒绝）。
5. 顶层只允许契约中的六个字段，出现未知字段即拒绝；`app_version`、`exported_at` 可以缺失，导入不校验它们的内容。
6. `settings` 必须包含白名单的全部六个键，缺少任何一个都拒绝；出现白名单之外的键同样拒绝（不允许借导入把 `proxy_pass` 之类的字段塞进偏好文件）。
7. 字段类型严格：`theme` 只能是 `light` / `dark`；三个更新开关必须是真正的布尔值（字符串 `"false"` 不算）；`min_words` / `max_words` 接受整数、整数值浮点数或纯数字字符串，范围 `0`–`1,000,000,000`，且最小值不得大于最大值；`updated_after` 必须是 `YYYY-MM-DD`；`search_filters` 与 `search_sort` 内部出现未知键同样拒绝，`search_sort` 必须同时包含 `sort` 与 `order`，且 `sort` / `order` 必须在允许值内。
8. 错误信息显示在卡片状态行与 Toast 中，现有设置保持原样。

**导入是合并写，不是恢复出厂**

`desktop_prefs.json` 中的其他键（包括不在白名单内的历史键、代理密码等）不会被删除；导入只覆盖 `settings` 中出现的那六个偏好键。因此导入一份「默认值」文件不会清空代理口令或账号信息。

写入前，若偏好文件已存在，会先把它复制为同目录的 `<文件名>.bak`（例如 `desktop_prefs.json.bak`），复制本身也是原子写；随后偏好文件通过临时文件 + 原子替换写入。若最终写入失败，旧文件保持原样，`.bak` 仍在，可用它回滚。

**换机迁移的局限**

* 只迁移上面六个桌面偏好。输出目录、代理与账号、音乐 token、界面语言都不在文件里，需要在新机器上手动设置。
* 导入不检查 `app_version`，但会拒绝 `format_version` 不等于 `1` 的文件；将来格式升级后，旧导出需要在新版本中重新导出，而不是手工改版本号。
* `.bak` 只有一份：每次走到写入阶段的导入都会覆盖它。它用于回滚最近一次导入，不是历史归档。
* 导入成功后界面会立即刷新主题与设置卡片，并重新拉取任务 / 关注列表，但**不会重启进程**；界面语言本来就不在导入范围内。

### general 配置

全局运行时设置, 面向下载 / 并发 / 速率限制 / 目录与存储的通用开关

#### 主配置项

| 参数名                | 类型    | 默认值             | 说明                                       |
| -------------------- | ------- | ----------------- | ------------------------------------------ |
| `raw_data_dir`       | `str`   | `"./raw_data"`    | 书籍数据存放目录                             |
| `output_dir`         | `str`   | `"./downloads"`   | 最终导出文件目录                             |
| `cache_dir`          | `str`   | `<用户数据目录>/cache` | 本地缓存目录 (字体 / 图片等); 相对路径按当前工作目录解析 |
| `request_interval`   | `float` | 0.5               | **同一本书**章节请求的间隔 (秒)               |
| `workers`            | `int`   | 4                 | 下载任务协程数量                             |
| `max_connections`    | `int`   | 10                | 最大并发连接数                               |
| `max_rps`            | `float` | 1000.0            | 全局 RPS 上限 (requests per second), 页面与图片/字体请求共享 |
| `retry_times`        | `int`   | 3                 | 请求重试次数 (只重试超时/连接错误与 408/425/429/5xx; 404/403 等确定性状态不重试, 并遵守服务端 `Retry-After`) |
| `backoff_factor`     | `float` | 2.0               | 重试的退避因子 (每次重试等待时间将按倍数增加, 如 `2s`, `4s`, `8s`) |
| `timeout`            | `float` | 10.0              | 单次请求超时 (秒)                            |
| `storage_batch_size` | `int`   | 1                 | `sqlite` 每批提交的章节数 (提高写入性能)       |
| `cache_book_info`    | `bool`  | `true`            | 是否启用 book_info 缓存                      |
| `cache_chapter`      | `bool`  | `true`            | 是否启用章节缓存                             |
| `fetch_inaccessible` | `bool`  | `false`           | 是否尝试获取未订阅章节                        |
| `login_required`     | `bool`  | `false`           | 全局默认是否需要登录; 站点级 `login_required` 可覆盖 |
| `strict_config`      | `bool`  | `false`           | 为 `true` 时处理器表中的未知选项直接报错, 而不是仅告警 |
| `backend`            | `str`   | `"aiohttp"`       | 全局 HTTP 请求后端, 可选 `"aiohttp"`, `"httpx"`, `"curl_cffi"` |
| `http2`              | `bool`  | `true`            | 仅对 `httpx` 生效, 启用 HTTP/2 支持           |
| `impersonate`        | `str/None` | `None`         | 仅对 `curl_cffi` 生效, 启用浏览器指纹仿真模式  |
| `browser_assisted_sites` | `list[str]` | `["fanqienovel"]` | 搜索时必须借助真实浏览器的站点键列表; 显式设为 `[]` 可禁用 |
| `download_retry_times` | `int`  | 2                 | 整本下载任务的额外重试次数 (仅对可重试错误生效); 请求层重试由 `retry_times` 负责 |
| `proxy`              | `str/None` | `None`         | HTTP/HTTPS 代理地址, 如 `"http://127.0.0.1:7890"` |
| `proxy_user`         | `str/None` | `None`         | 代理用户名                                    |
| `proxy_pass`         | `str/None` | `None`         | 代理密码                                      |
| `trust_env`          | `bool`  | `false`           | 是否读取系统代理等环境变量                     |
| `verify_ssl`         | `bool`  | `true`            | 是否校验 TLS 证书 (排查代理问题时才考虑关闭)   |
| `locale_style`       | `str`   | `"simplified"`    | 站点简/繁体镜像选择, 可选 `"simplified"` / `"traditional"` (仅对提供双镜像的站点生效) |
| `update_check`       | `str`   | `"auto"`          | 更新检查策略: `"auto"` (命令结束后静默检查, 每天最多一次) / `"manual"` (仅运行 `novel-cli update` 时检查) / `"off"` (从不检查) |
| `user_agent`         | `str/None` | `None`         | 自定义请求 User-Agent (覆盖默认值)             |
| `headers`            | `dict/None` | `None`        | 附加自定义请求头 (键值对)                      |

**缓存目录**

`cache_dir` 未设置时, 缓存 (含站点 Cookie) 保存在应用数据目录下的 `cache/` 子目录: Windows 为 `%LOCALAPPDATA%\novel_downloader\cache`, Linux 为 `~/.local/share/novel_downloader/cache`; 应用数据目录可通过环境变量 `NOVEL_HOME` 覆盖。显式设置的相对路径仍按当前工作目录解析, 如需旧版行为可设置 `cache_dir = "./novel_cache"`。

**站点压力与 503**

部分站点对高频访问敏感 (例如 >= 5 RPS), 可能返回 `503 Service Temporarily Unavailable`。

建议适当**降低** `max_rps` 或**增大** `request_interval`; 工具支持断点续爬, 已完成的数据不会重复抓取。

**HTTP 请求后端**

程序支持可插拔式 HTTP 后端, 可在 `[general]` 中通过 `backend` 参数进行切换:

| 后端名称     | 说明                                                | 依赖安装                    |
| ----------- | --------------------------------------------------- | -------------------------- |
| `aiohttp`   | 默认后端, 基于 `aiohttp` 的异步 HTTP 客户端           | -                          |
| `httpx`     | 现代异步 HTTP 客户端, 支持 HTTP/1.1 与 HTTP/2         | `pip install httpx[http2]` |
| `curl_cffi` | 基于 `libcurl` 的实现, 支持浏览器仿真 (`impersonate`) | `pip install curl_cffi`    |

> **扩展参数**
>
> * `http2`: 仅对 `httpx` 生效, 启用 HTTP/2 支持 (默认 `true`)
> * `impersonate`: 仅对 `curl_cffi` 生效, 启用浏览器指纹仿真, 可设为 `"chrome136"`, `"chrome"` 等
>
> 更多信息请参考各项目文档:
>
> * [`httpx`](https://github.com/encode/httpx)
> * [`curl_cffi`](https://github.com/lexiforest/curl_cffi)

**浏览器辅助搜索**

少数站点的搜索请求由站点自身的 JavaScript 生成签名, 纯 HTTP 请求拿不到结果。把这些站点写入 `browser_assisted_sites` 后, 显式搜索该站点时会复用登录会话启动隐藏浏览器, 由站点自身脚本发起请求并截获结果; 聚合搜索仍会跳过它们。

```toml
[general]
# 默认值即 ["fanqienovel"]
browser_assisted_sites = ["fanqienovel"]
```

* 也接受逗号分隔的字符串: `browser_assisted_sites = "fanqienovel,example"`。
* 省略该项时使用默认值 `["fanqienovel"]`。
* 显式设为 `browser_assisted_sites = []` 会完全禁用浏览器辅助搜索, 这些站点将回退为普通 HTTP 搜索 (通常得不到结果)。
* 使用前请先 `novel-cli login <site>` 建立登录会话, 并安装可选浏览器组件。

**兼容性与 Cloudflare 行为说明**

由于各 HTTP 客户端在协议实现与 TLS 指纹上的差异, 部分站点在使用默认的 `aiohttp` 后端时, 可能会触发 **Cloudflare 反爬虫或访问验证机制**, 从而导致连接被拦截或返回 `403`/`5xx` 状态码。

若遇到此类情况, 建议切换至以下后端之一:

* **`httpx` (启用 `http2 = true`)**: 通过 HTTP/2 可获得更接近现代浏览器的网络特征, 对部分启用 Cloudflare 的站点有更好的兼容性
* **`curl_cffi` (启用 `impersonate`)**: 通过模拟浏览器 TLS 指纹和请求头, 可显著提升被 Cloudflare 或其他 WAF 拦截的站点的访问成功率

需要注意的是, 不同站点的后端表现可能存在差异。例如:

* 某些站点仅在 `aiohttp` 或 `httpx` 的 HTTP/1.1 模式下可正常访问, 而在启用 `http2 = true` 时反而会触发 Cloudflare 或导致连接失败
* 另一些站点则恰好相反, 只有在启用 HTTP/2 或使用 `curl_cffi` 的浏览器仿真模式下才能成功建立连接

因此, 在遇到访问异常 (如 403、TLS 握手失败、Cloudflare 验证循环等) 时, 建议根据具体站点情况灵活调整后端和相关参数。

#### parser 子节

该配置用于处理 **混淆字体解码** 与 **图片章节 OCR 识别**, 并控制图片章节的 **去水印**。

| 参数名             | 类型            | 默认值      | 说明                                                        |
| ------------------ | -------------- | ---------- | ----------------------------------------------------------- |
| `enable_ocr`       | `bool`         | false      | 是否启用本地 OCR, 用于识别混淆字体或图片章节文本                |
| `batch_size`       | `int`          | 32         | OCR 模型推理时的批处理大小                                    |
| `remove_watermark` | `bool`         | false      | 是否尝试对图片章节进行去水印 (部分站点支持)                     |
| `model_name`       | `str/None`     | None       | OCR 模型名称; 为 `None` 时由 PaddleOCR 选择 `PP-OCRv5_server_rec`。自带 `settings.toml` 显式设为 `PP-OCRv5_mobile_rec` (CPU 上快得多) |
| `model_dir`        | `str/None`     | None       | OCR 模型存储路径                                              |
| `input_shape`      | `tuple/None`   | None       | OCR 模型输入图像尺寸, 格式为 (C, H, W)                         |
| `device`           | `str/None`     | None       | 用于推理的设备, 例如: "cpu"、"gpu"、"npu"、"gpu:0"、"gpu:0,1"  |
| `precision`         | `str`          | `"fp32"`   | 推理精度                                       |
| `cpu_threads`      | `int`          | 10         | 在 CPU 上推理时使用的线程数量                                  |
| `enable_hpi`       | `bool`         | false      | 是否启用高性能推理                                             |
| `use_truncation`   | `bool`         | true       | 是否对重复/截断的章节内容做截断处理 (如起点重复段落)             |
| `cut_mode`         | `str`          | `"none"`   | 章节内容裁剪模式                                               |

功能说明:

**混淆字体章节**

* 若未开启解析或解析失败, 程序将在导出 EPUB/HTML 时自动嵌入对应字体文件, 确保显示正常

依赖说明:

若启用了 `enable_ocr`, 需安装额外的图像处理依赖

```bash
pip install novel-downloader[image-utils]
```

OCR 功能依赖 `PaddleOCR` 及其模型, 请参考安装指南:

* [安装说明](./installation.md)

`PaddleOCR` 配置参考官方文档:

* [PaddleOCR 文档](https://www.paddleocr.ai/main/version3.x/module_usage/text_recognition.html#_4)

#### output 子节

控制导出格式, 文件命名与 EPUB 细节

| 参数名                        | 类型         | 默认值                | 说明                                       |
| ----------------------------- | ----------- | --------------------- | ------------------------------------------ |
| `formats`                     | `list[str]` | `[]`                  | 输出格式; 未设置时 CLI 按 `epub` 导出         |
| `render_missing_chapter`      | `bool`      | `true`                | 是否在导出时为缺失章节插入占位内容            |
| `append_timestamp`            | `bool`      | `true`                | 输出文件名是否追加时间戳                     |
| `filename_template`           | `str`       | `"{title}_{author}"`  | 文件名模板                                  |
| `include_picture`             | `bool`      | `true`                | 是否嵌入章节中的图片 (可能增加文件体积)       |
| `split_mode`                  | `str`       | `"book"`              | 拆分导出模式: `book` (整本书一个文件) / `volume` (按卷) / `chapter` (按章) |

#### 调试子节

| 参数名                | 类型    | 默认值             | 说明                                  |
| -------------------- | ------- | ----------------- | ------------------------------------- |
| `debug.save_html`    | `bool`  | `false`           | 是否保存抓取到的原始 HTML 到磁盘         |
| `debug.log_level`    | `str`   | `"INFO"`          | 日志级别: DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `debug.log_dir`      | `str`   | `"./logs"`        | 运行日志存放目录                        |

#### 示例配置

以下取值与 `novel-cli config init` 生成的默认配置一致, 可按需修改:

```toml
[general]
retry_times = 3
backoff_factor = 2.0
timeout = 10.0
max_connections = 10
max_rps = 1000.0
request_interval = 0.5
raw_data_dir = "./raw_data"
output_dir = "./downloads"
# cache_dir 默认位于应用数据目录下的 cache/, 如需自定义 (相对路径按当前工作目录解析):
# cache_dir = "./novel_cache"
workers = 4
cache_book_info = true
cache_chapter = true
storage_batch_size = 1

[general.output]
formats = [
    "txt",
    "epub",
]
include_picture = true

[general.parser]
enable_ocr = false
batch_size = 32
remove_watermark = false

model_name = "PP-OCRv5_mobile_rec"
```

---

### sites 配置

站点级设置 (如 `qidian`, `b520`, ...), **站点级会覆盖全局行为**, 每个站点配置位于 `[sites.<site>]` 下

#### 通用键

| 参数名             | 类型                                  | 默认值 | 说明                                   |
| ----------------- | ------------------------------------- | ------ | ------------------------------------- |
| `book_ids`        | `list<str>` / `list<dict>`            | -      | 小说 ID 列表                           |
| `login_required`  | `bool`                                | false  | 是否需要登录才能访问                    |

#### `book_ids` 字段说明

`book_ids` 字段支持以下两种格式:

1) 简单列表

```toml
[sites.<site>]
book_ids = [
  "1010868264",
  "1020304050"
]
```

2) 结构化 (支持范围与忽略列表)

```toml
[sites.<site>]
login_required = true

[[sites.<site>.book_ids]]
book_id = "1030412702"
start_id = "833888839"
end_id = "921312343"
ignore_ids = ["1234563", "43212314"]

[[sites.<site>.book_ids]]
book_id = "1111111111"   # 其他字段可省略
```

**结构化字段说明**

| 字段名        | 类型          | 必需 | 说明                        |
| ------------ | ------------- | --- | --------------------------- |
| `book_id`    | `str`         | 是  | 小说的唯一标识 ID             |
| `start_id`   | `str`         | 否  | 起始章节 ID (含起)            |
| `end_id`     | `str`         | 否  | 结束章节 ID (含止)            |
| `ignore_ids` | `list[str]`   | 否  | 需要跳过的章节 ID             |

#### 示例配置

**简单格式**

```toml
[sites.qidian]
book_ids = [
  "1010868264",
  "1012584111"
]
login_required = true

# 站点级 parser 会覆盖 [general.parser] 中的同名项
[sites.qidian.parser]
use_truncation = true
```

> `use_truncation` 属于解析器选项, **必须**写在 `[sites.<site>.parser]` (或 `[general.parser]`) 下。
> 写在 `[sites.<site>]` 顶层不会被读取; 配置校验会给出告警, 并建议迁移到 `sites.<site>.parser.use_truncation`。
> 其它解析器选项 (`enable_ocr`、`remove_watermark`、`cut_mode` 等) 同样可以在站点级 `parser` 表中覆盖。

**结构化格式**

```toml
[sites.qidian]
login_required = true

[[sites.qidian.book_ids]]
book_id = "1010868264"
start_id = "434742822"
end_id = "528536599"
ignore_ids = ["507161874", "516065132"]

[[sites.qidian.book_ids]]
book_id = "1012584111"
```

**站点覆盖全局行为**

站点配置可以覆盖全局配置。

如下例中, 全局的 `request_interval = 0.5` 会被 `sites.qidian` 中的 `request_interval = 2.0` 覆盖:

```toml
[general]
timeout = 10.0
request_interval = 0.5

[sites.qidian]
login_required = true
request_interval = 2.0  # 覆盖全局的 0.5
```

---

### plugins 配置

插件系统相关设置, 用于开启/覆盖内置实现。

#### 主配置项

| 参数名                  | 类型     | 默认值               | 说明                                          |
| ---------------------- | -------- | ------------------- | --------------------------------------------- |
| `enable_local_plugins` | `bool`   | `false`             | 是否启用本地插件目录 (扫描并加载本地插件)                          |
| `override_builtins`    | `bool`   | `false`             | 是否允许本地插件**覆盖**同名的内置实现 (如同名站点/处理器/导出器)           |
| `local_plugins_path`   | `str`    | `"./novel_plugins"` | 本地插件目录路径; 仅在 `enable_local_plugins = true` 时生效 |

> 更多插件编写说明 (自定义站点、处理器、导出器), 见: [插件系统文档](../reference/plugins.md)

#### 示例配置

```toml
[plugins]
# 是否启用本地插件目录
enable_local_plugins = false
# 是否允许本地插件覆盖内置实现
override_builtins = false
# 本地插件路径 (可选)
local_plugins_path = "./novel_plugins"
```

---

### 配置校验与迁移

`novel_downloader.infra.config` 包导出了两个配置工具函数, 可在加载 `settings.toml` 后自行检查或迁移旧键:

| 函数             | 签名                                        | 说明                                                                                     |
| ---------------- | ------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `validate_config` | `(path_or_dict) -> list[Problem]`           | 校验配置映射或 `.toml`/`.json` 配置文件路径, 返回问题列表 (空列表表示配置合法); 解析错误与文件不存在以 `error` 问题返回而不会抛出异常, 也不访问网络 |
| `migrate_config` | `(data) -> tuple[dict, list[str]]`          | 将旧键迁移到当前布局, 返回 `(迁移后的配置, 变更说明列表)`                                   |

`Problem` 对象包含 `level` (`error` / `warning` / `info`)、`path` (配置内的点号路径)、`message` (描述) 与 `fix_hint` (修复建议) 字段。

`migrate_config` 目前执行的迁移:

* `sites.<site>.cookie` -> `sites.<site>.cookies`
* `sites.<site>.use_truncation` -> `sites.<site>.parser.use_truncation`
* `[general]` 表存在但未设置 `download_retry_times` 时填充默认值 `2`

该函数幂等: 对同一配置重复调用, 第二次会得到相同数据与空的变更列表; 输入映射不会被修改。

```python
from novel_downloader.infra.config import migrate_config, validate_config

problems = validate_config("settings.toml")
for p in problems:
    print(p)  # [level] path: message Hint: fix_hint

cfg, notes = migrate_config(cfg)
for note in notes:
    print(note)
```
