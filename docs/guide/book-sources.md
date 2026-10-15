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

# 安装到用户目录 ($NOVEL_RULES_DIR, 未设置时用用户配置目录); --force 覆盖同名文件
novel-cli sources add ./my-site.json

# 卸载用户书源 (内置书源受保护, 无法删除)
novel-cli sources remove my-site

# 使用规则书源搜索 / 下载
novel-cli search "斗破苍穹" -s biquge5r
novel-cli download --site biquge5r 0_143 --start 155832 --end 155835

# 脚本化用法: 结果以 JSON 打到 stdout; --first 直接下载排名第一的结果
novel-cli search "斗破苍穹" --json
novel-cli search "斗破苍穹" --first
```

`--json` 会关闭交互提示, 并把机器可读的结果输出到 stdout; 表格、日志等诊断信息一律走 stderr, 因此可以直接管道给 `jq`。`--json` 与 `--first` 不能同时使用。

文件解析失败 (JSON 语法错误、缺少必填键、正则非法等) 时会在加载阶段被跳过并记录日志, 不会影响其它书源。

---

### 顶层字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `site_key` | string | 是 | 站点唯一标识; 纯数字会被自动加 `n` 前缀 |
| `site_name` | string | 否 | 展示名称, 缺省时回退为 `site_key` |
| `base_url` | string | 否 | 站点根地址, 供 `{base_url}` 模板变量使用 |
| `encoding` | string | 否 | 页面编码, 默认 `utf-8` |
| `format` | string | 否 | 站点默认响应格式: `html` (默认) 或 `json`; 各段可单独覆盖 |
| `priority` | int | 否 | 书源优先级, 默认 `200` |
| `hosts` | string[] | 否 | 命中这些主机名的链接交给本规则解析 (配合 `resolve`) |
| `headers` | object | 否 | 所有请求共用的请求头 |
| `resolve` | object | 否 | 链接解析规则, 见下方 |

`book` 与 `chapter` 至少提供一个。

站点若整体走 AJAX/JSON 接口, 在顶层写 `"format": "json"` 即可, 无需给 `search` / `book` / `chapter` 逐段重复声明 (逐段声明会覆盖顶层值)。

---

### search: 搜索

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `url` | string | — | 搜索页地址, 必填 |
| `method` | string | `GET` | `GET` 或 `POST` |
| `data` | object | `{}` | POST 表单体, 值支持模板变量 |
| `json_body` | object | 空 | POST JSON 请求体 (自动带 `Content-Type: application/json`), 字符串值支持模板变量与 `data` 互斥使用 |
| `format` | string | 继承 | 本节响应格式, 覆盖顶层 `format` |
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
| `format` | string | 继承 | 本节响应格式, 覆盖顶层 `format` |
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
| `format` | string | 继承 | 本节响应格式, 覆盖顶层 `format` |
| `headers` | object | `{}` | 本节独享的请求头 |

---

### pages: 分页

```json
"pages": { "url": "{base_url}/book/{book_id}/index_{page}.html", "start": 2 }
```

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `url` | string | — | 页码地址模板, 必填 |
| `start` | int | `2` | 起始页码 |
| `max_pages` | int | 空 | 指定后固定抓取 `start`~`max_pages` 页, 不再依赖“下一页链接是否存在”的启发式 |

也可写成字符串简写。默认策略从 `start` 开始, 当**下一页链接仍出现在当前页 HTML 中**时继续, 最多额外抓取 50 页。JSON 接口通常不会在响应体里回链下一页, 这类站点应改用 `max_pages`。目录分页结果会自动按章节链接去重合并; 正文分页结果按顺序拼接。

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

`selector` 的语法取决于所在段的 `format`:

- **HTML (默认)** — 两种语法:
  - **CSS 选择器**: `.list`, `div#content`, `meta[property='og:image']`
  - **XPath**: 以 `/`、`./` 或 `(` 开头, 如 `//div[@class='item']`、`(//div[@class='item'])[2]`
- **JSON** (`"format": "json"`) — 点号路径, 见下方「JSON / AJAX 接口」一节

`attr` 取值:

| 值 | 含义 |
| --- | --- |
| `text` | 节点内所有文本拼接 (不含分隔符); JSON 下即“值本身” |
| `inner_text` | 节点内文本按行拼接 (适合正文, 保留段落); JSON 下数组会按 `\n` 拼接 |
| `html` | 节点的 HTML 源码; JSON 下等价于 `text` |
| 其它任意字符串 | 作为 HTML 属性名读取, 如 `href` / `src` / `content`; JSON 下作为对象的键读取 |

---

### 模板变量

| 变量 | 可用位置 | 说明 |
| --- | --- | --- |
| `{base_url}` | URL、`data` / `json_body` 值、`headers` 值 | 站点根地址 (已去掉末尾 `/`) |
| `{book_id}` | URL、`data` / `json_body` 值、`headers` 值 | 书籍 ID |
| `{chapter_id}` | URL、`data` / `json_body` 值、`headers` 值 | 章节 ID |
| `{page}` | 分页 URL、`data` / `json_body` 值 | 分页页码 |
| `{keyword}` | 搜索 URL、`data` / `json_body` 值 | 搜索关键词, 已 URL 编码 |
| `{raw_keyword}` | 搜索 URL、`data` / `json_body` 值 | 搜索关键词原样 |

未识别的 `{...}` 会原样保留, 不会被替换。`json_body` 中的模板只作用于字符串值。

---

### JSON / AJAX 接口

把 `format` 设为 `json` 后, 各段的 `selector` 改为点号路径, 并可直接解析 AJAX 接口返回的 JSON。

| 写法 | 含义 |
| --- | --- |
| `data.list` | 取 `data` 对象的 `list` 键; 若结果本身是数组, 会自动展开为各元素 |
| `data.list[0]` | 显式下标取值 (此写法不再自动展开) |
| `data.list[*]` | 展开数组的全部元素 |
| `data.list.title` | 对数组元素逐个取 `title` (数组上的隐式展开) |
| `$.data.total` | 前导 `$` 与 `.` 可省略 |

路径末尾若是数组且没有显式下标, 会被展开; 这正是 `item` 选择器应写成的形式 (`"item": "data.list"` 得到的是列表里的每一条, 而不是整个列表)。

示例 (搜索走 POST JSON 接口, 详情与正文仍是 HTML):

```json
{
  "site_key": "demo-json",
  "base_url": "https://api.demo.example",
  "format": "html",

  "search": {
    "url": "https://api.demo.example/search",
    "method": "POST",
    "format": "json",
    "json_body": {"kw": "{raw_keyword}", "page": "{page}"},
    "item": "data.list",
    "fields": {
      "title": {"selector": "name", "attr": "text"},
      "url": {"selector": "url", "attr": "text"},
      "author": {"selector": "author", "attr": "text"},
      "cover": {"selector": "coverUrl", "attr": "text"},
      "tags": {"selector": "tags[*]", "attr": "text", "multi": true}
    }
  },

  "book": {
    "url": "{base_url}/book/{book_id}.html",
    "fields": {
      "book_name": {"selector": "h1", "attr": "text"},
      "tags": {"selector": "span.tag", "attr": "text", "multi": true}
    },
    "chapters": {
      "item": "div.list a",
      "title": {"attr": "text"},
      "url": {"attr": "href"}
    }
  },

  "chapter": {
    "url": "{base_url}/read/{book_id}/{chapter_id}.html",
    "content": {"selector": "div#content", "attr": "inner_text"}
  }
}
```

JSON 接口分页时, 由于响应体通常不回链下一页, 请在 `pages` 上使用 `max_pages`:

```json
"pages": {"url": "https://api.demo.example/book/{book_id}?page={page}", "start": 2, "max_pages": 20}
```

---

### 内置示例

`biquge5r.json` 是一个完整可用的内置书源, 可作为编写新规则的起点; `rules/_example-template.json` 覆盖了全部字段 (含 JSON 模式写法)。

---

### 已知限制

- 规则书源的 JSON 模式只做**取值**, 不含逻辑与脚本; 需要签名、加密或复杂计算的接口仍建议编写 Python 插件
- 请求频率由客户端配置统一控制 (见 [配置说明](./configuration.md)), 规则内不提供独立的节流参数
- 目录与正文分页最多额外抓取 50 页 (`max_pages` 同样受此上限约束)
- 部分镜像站点可用性会波动, 搜索无结果时可换其它书源重试
