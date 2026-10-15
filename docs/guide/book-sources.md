## 规则书源

除内置的 Python 站点插件外, 本项目支持用一份 JSON 规则描述一个书源: 搜索、书籍详情、目录、正文、分页与 URL 解析全部由规则驱动, 新增站点无需改动代码。

---

### 加载位置

规则文件从以下位置自动加载, **后加载者覆盖前者** (同 `site_key` 时):

| 位置 | 说明 |
| --- | --- |
| `novel_downloader/plugins/rule/builtin/*.json` | 随项目内置的书源 |
| `./rules/*.json` | 运行目录下的自定义书源 |
| `$NOVEL_RULES_DIR/*.json` | 环境变量指定的目录 |
| `<用户配置目录>/novel_downloader/rules/*.json` | 每个用户的私有书源 |

文件名以 `_` 开头的规则文件会被跳过, 适合存放模板与草稿。

仓库中的 [`rules/_example-template.json`](https://github.com/lingyi-ling/novel-downloader/blob/main/rules/_example-template.json) 是一个覆盖全部字段的示例模板 (因带 `_` 前缀默认不会被加载), 可直接复制并去掉下划线后使用。

---

### 校验与管理

```bash
# 列出所有规则书源 (--check 会额外校验能否成功注册)
novel-cli sources list --check

# 校验单个规则文件
novel-cli sources check ./rules/_example-template.json

# 使用规则书源搜索 / 下载
novel-cli search "斗破苍穹" -s biquge5r
novel-cli download --site biquge5r 0_143 --start 155832 --end 155835
```

规则文件解析失败 (JSON 语法错误、缺少必填键、正则非法等) 时会在加载阶段被跳过并记录日志, 不会影响其它书源。

---

### 顶层字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `site_key` | string | 是 | 站点唯一标识; 纯数字会被自动加 `n` 前缀 |
| `site_name` | string | 否 | 展示名称, 缺省时回退为 `site_key` |
| `base_url` | string | 否 | 站点根地址, 供 `{base_url}` 模板变量使用 |
| `encoding` | string | 否 | 页面编码, 默认 `utf-8` |
| `priority` | int | 否 | 书源优先级, 默认 `200` |
| `hosts` | string[] | 否 | 命中这些主机名的链接交给本规则解析 (配合 `resolve`) |
| `headers` | object | 否 | 所有请求共用的请求头 |
| `resolve` | object | 否 | 链接解析规则, 见下方 |

`book` 与 `chapter` 至少提供一个。

---

### search: 搜索

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `url` | string | — | 搜索页地址, 必填 |
| `method` | string | `GET` | `GET` 或 `POST` |
| `data` | object | `{}` | POST 表单体, 值支持模板变量 |
| `headers` | object | `{}` | 本节独享的请求头 |
| `item` | string | 空 | 结果条目的选择器; 留空表示整页即为一个结果 |
| `fields` | object | `{}` | 字段提取规则, 键名含义见下表 |
| `book_id_regex` | string | 空 | 从书籍链接中提取 ID 的正则, 用 `search` 语义匹配 |
| `book_id_group` | int | `1` | `book_id_regex` 的捕获组序号 |
| `priority` | int | `200` | 该结果的排序权重, 第 N 个结果加 N |

`fields` 中可用的键: `title` / `url` / `cover` / `author` / `latest` / `update` / `word`。其中 `url` 为必填, 缺失该字段的条目会被丢弃。

---

### book: 详情与目录

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `url` | string | — | 详情页地址模板, 必填 |
| `fields` | object | `{}` | 详情字段, 支持 `book_name` / `author` / `cover_url` / `summary` / `tags` 等 |
| `chapters` | object | — | 目录规则, 必填 |
| `pages` | object \| string | 空 | 目录分页规则 |
| `headers` | object | `{}` | 本节独享的请求头 |

`chapters`:

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `item` | string | — | 章节目录条目的选择器, 必填 |
| `title` | field | `{"attr": "text"}` | 章节标题提取规则 |
| `url` | field | `{"attr": "href"}` | 章节链接提取规则 |
| `id_regex` | string | 空 | 从链接中提取章节 ID 的正则; 缺省时取链接文件名去扩展名 |
| `id_group` | int | `1` | `id_regex` 的捕获组序号 |
| `volume_name` | string | `正文` | 归入的分卷名 |

---

### chapter: 正文

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `url` | string | — | 正文页地址模板, 必填 |
| `content` | field | `{"selector": "body"}` | 正文提取规则 |
| `title` | field | 空 | 章节标题提取规则 |
| `replace` | [string, string][] | `[]` | 逐条替换, 按顺序执行 |
| `drop` | string[] | `[]` | 命中任一正则的行会被整行删除 |
| `pages` | object \| string | 空 | 正文分页规则 |
| `headers` | object | `{}` | 本节独享的请求头 |

---

### pages: 分页

```json
"pages": { "url": "{base_url}/book/{book_id}/index_{page}.html", "start": 2 }
```

也可写成字符串简写。抓取从 `start` 开始, 当**下一页链接仍出现在当前页 HTML 中**时继续, 最多额外抓取 50 页。目录分页结果会自动按章节链接去重合并; 正文分页结果按顺序拼接。

---

### field: 字段提取

`field` 可以是一个字符串 (等价于 `{"selector": "..."}`), 也可以是一个对象:

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `selector` | string | 空 | 选择器; 留空表示直接使用当前节点 |
| `attr` | string | `text` | 取值方式, 见下 |
| `regex` | string | 空 | 对取到的值再做一次正则提取 (跨行匹配) |
| `group` | int | `0` | 正则捕获组; 组号越界时回退到整段匹配 |
| `multi` | bool | `false` | 是否收集全部匹配项 (返回数组) |
| `replace` | [string, string][] | `[]` | 逐条替换 |
| `default` | string | 空 | 未匹配到任何节点时的兜底值 |

`selector` 支持两种语法:

- **CSS 选择器**: `.list`, `div#content`, `meta[property='og:image']`
- **XPath**: 以 `/`、`./` 或 `(` 开头, 如 `//div[@class='item']`、`(//div[@class='item'])[2]`

`attr` 取值:

| 值 | 含义 |
| --- | --- |
| `text` | 节点内所有文本拼接 (不含分隔符) |
| `inner_text` | 节点内文本按行拼接 (适合正文, 保留段落) |
| `html` | 节点的 HTML 源码 |
| 其它任意字符串 | 作为 HTML 属性名读取, 如 `href` / `src` / `content` |

---

### 模板变量

| 变量 | 可用位置 | 说明 |
| --- | --- | --- |
| `{base_url}` | URL、`data` 值、`headers` 值 | 站点根地址 (已去掉末尾 `/`) |
| `{book_id}` | URL、`data` 值、`headers` 值 | 书籍 ID |
| `{chapter_id}` | URL、`data` 值、`headers` 值 | 章节 ID |
| `{page}` | 分页 URL | 分页页码 |
| `{keyword}` | 搜索 URL、`data` 值 | 搜索关键词, 已 URL 编码 |
| `{raw_keyword}` | 搜索 URL、`data` 值 | 搜索关键词原样 |

未识别的 `{...}` 会原样保留, 不会被替换。

---

### resolve: 直接粘贴链接下载

```json
"resolve": {
  "book": "^/book/(\\d+)/?$",
  "chapter": "^/book/(\\d+)/(\\d+)\\.html$"
}
```

`hosts` 中的主机名命中后, 用 URL 的 path 去匹配 `resolve.chapter`, 再匹配 `resolve.book`。`chapter` 正则的第 1 组为 `book_id`, 第 2 组为 `chapter_id`。

已有内置 Python 插件优先于规则书源。

---

### 内置示例

`biquge5r.json` 是一个完整可用的内置书源, 可作为编写新规则的起点。

---

### 已知限制

- 规则书源只解析服务端返回的 HTML, **不支持需要 JavaScript 渲染或 JSON 接口的站点**; 这类站点仍建议编写 Python 插件
- 请求频率由客户端配置统一控制 (见 [配置说明](./configuration.md)), 规则内不提供独立的节流参数
- 目录与正文分页最多额外抓取 50 页
