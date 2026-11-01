## 规则 DSL 参考

本文是规则书源 JSON 的**字段级参考**, 与 [书源](./book-sources.md) 一文互补: 后者面向"如何新增一个书源", 本文面向"每个键到底控制什么"。

> **与代码同步**: 本文的字段表带有 `rule-dsl:keys:*` 注释标记, `tests/plugins/test_rule_docs.py` 会解析这些表格, 与 `novel_downloader.plugins.rule.model` 中的真实集合 (`_TOP_LEVEL_KEYS` / `_SEARCH_KEYS` / `_FIELD_KEYS` / `_STEP_KEYS` / `_LIMIT_KEYS` 等) 比对。新增或删除字段时, 必须同时修改本文, 否则测试失败并列出差异。
>
> 本文以当前代码为准。示例规则位于 `rules/_example-advanced-html.json` 与 `rules/_example-json-api.json`, 均以 `_` 开头, 不会被加载器注册为真实站点。

---

### 加载与作用域

| 位置 | 说明 |
| --- | --- |
| `novel_downloader/plugins/rule/builtin/*.json` | 随项目内置的书源, 自动加载 |
| `./rules/*.json` | 运行目录下的自定义书源 |
| `$NOVEL_RULES_DIR/*.json` | 环境变量指定的目录 |
| `<用户配置目录>/novel_downloader/rules/*.json` | 每个用户的私有书源 |

后加载者覆盖前者 (同 `site_key` 时)。

**在 `rules/` 等用户目录下, 以 `_` 开头的文件名不会被加载。** 示例与草稿都放在 `rules/` 并加 `_` 前缀:

- `rules/_example-template.json` —— 全字段模板;
- `rules/_example-advanced-html.json` —— HTML 站点: steps / fallback / preprocess / limits / 分页守卫;
- `rules/_example-json-api.json` —— JSON 接口 + 单结果回退。

不要把示例放进 `builtin/`: 该目录下的每个 `*.json` 都会被当成真实书源注册, 示例站点不存在, 会污染搜索与 `sources list`。

两种 `_` 约定不要混淆:

- **文件名**以 `_` 开头: 整个文件不加载;
- **键名**以 `_` 开头: 在规则的结构层 (顶层与各 section) 被忽略, 用作注释。`data` / `json_body` / `headers` 这类自由键值表的内部不受此约定影响。

---

### 顶层字段

<!-- rule-dsl:keys:top_level -->
| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `site_key` | string | — | 必填, 站点唯一标识; 纯数字会自动加 `n` 前缀 |
| `site_name` | string | `""` | 展示名称, 缺省时回退为 `site_key` |
| `base_url` | string | `""` | 站点根地址, 供 `{base_url}` 模板变量使用 (已去末尾 `/`) |
| `encoding` | string | `utf-8` | 页面编码 |
| `format` | string | `html` | 默认响应格式, 只能是 `html` 或 `json`; 各段可覆盖 |
| `priority` | integer | `200` | 书源优先级, 数值越大越靠前 |
| `hosts` | string[] | `[]` | 命中这些主机名 (小写) 的链接交给本规则解析, 配合 `resolve` |
| `headers` | object | `{}` | 请求头; 作用于 book / chapter 抓取, **原样发送, 不做模板替换**(需要变量时写在各段的 `headers` 里) |
| `resolve` | object | 空 | 链接 → `book_id` / `chapter_id` 解析规则 |
| `search` | object | 空 | 搜索段 |
| `book` | object | 空 | 详情 + 目录段 |
| `chapter` | object | 空 | 正文段 |
| `limits` | object | 空 | 规则级并发 / 间隔 / 重试覆盖, 见下方 |
<!-- /rule-dsl:keys:top_level -->

`book` 与 `chapter` 至少提供一个, 否则规则视为无效。请求头中出现 CR / LF / NUL 会直接判为非法, 用于阻断头注入。

---

### `search`: 搜索

<!-- rule-dsl:keys:search -->
| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `url` | string | — | 必填, 搜索地址; 支持 `{base_url}` / `{keyword}` / `{raw_keyword}` / `{page}` |
| `method` | string | `GET` | `POST` 走表单提交; 其它值 (含 `GET`) 一律按 GET 处理; 方法名会转大写, 不做白名单校验 |
| `data` | object | `{}` | POST 表单体, 字符串值支持 `{keyword}` / `{raw_keyword}` / `{page}` 模板 |
| `json_body` | object | 空 | POST JSON 请求体, 自动带 `Content-Type: application/json`; 提供时总是以 JSON POST 发送并优先于 `data` / `method` |
| `headers` | object | `{}` | 搜索专用请求头; **原样发送, 不做模板替换** |
| `format` | string | 继承顶层 | 本节响应格式, 覆盖顶层 `format` |
| `item` | string | `""` | 结果条目选择器; 留空表示整页即一条结果 |
| `fields` | object | `{}` | 字段提取规则, 键名受限, 见下 |
| `book_id_regex` | string | 空 | 从书籍链接提取 ID 的正则 (`search` 语义) |
| `book_id_group` | integer | `1` | 上述正则的捕获组序号, 必须 >= 0 |
| `priority` | integer | `200` | 搜索结果的排序权重; 同一条搜索内按结果顺序依次加 `0`、`1`、`2`… |
| `single_result_fallback` | boolean | `false` | `item` 零匹配时把整页当作一条结果, 见下方 |
<!-- /rule-dsl:keys:search -->

`fields` 只接受引擎真正消费的键:

<!-- rule-dsl:keys:search_fields -->
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `title` | field | 书名 |
| `url` | field | 详情页链接, **必填**; 提取不到则该条结果被丢弃 |
| `cover` | field | 封面图, 相对地址会按 `base_url` 补全 |
| `author` | field | 作者 |
| `latest` | field | 最新章节名 |
| `update` | field | 更新时间 |
| `word` | field | 字数; 缺省时展示为 `-` |
<!-- /rule-dsl:keys:search_fields -->

---

### `book`: 详情与目录

<!-- rule-dsl:keys:book -->
| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `url` | string | — | 必填, 详情页模板, 支持 `{base_url}` / `{book_id}` |
| `fields` | object | `{}` | 详情字段, 键名受限, 见下 |
| `chapters` | object | — | 必填, 目录规则 |
| `pages` | object \| string | 空 | 目录分页规则, 字符串等价于 `{"url": "..."}` |
| `headers` | object | `{}` | 本节请求头, 值支持模板变量 |
| `format` | string | 继承顶层 | 本节响应格式 |
<!-- /rule-dsl:keys:book -->

详情字段名:

<!-- rule-dsl:keys:book_fields -->
| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `book_name` | field | 书名 |
| `author` | field | 作者 |
| `cover_url` | field | 封面, 相对地址会补全 |
| `update_time` | field | 更新时间 |
| `summary` | field | 简介 (完整) |
| `summary_brief` | field | 简介 (短), 仅非空时写入 |
| `serial_status` | field | 连载状态, 仅非空时写入 |
| `word_count` | field | 字数, 仅非空时写入 |
| `tags` | field | 标签, **唯一支持 `multi` 的字段**, 收集全部匹配项 |
<!-- /rule-dsl:keys:book_fields -->

`chapters`:

<!-- rule-dsl:keys:chapters -->
| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `item` | string | — | 必填, 目录条目选择器 |
| `title` | field | `{"attr": "text"}` | 章节标题 |
| `url` | field | `{"attr": "href"}` | 章节链接; 相对地址按 `base_url` 补全, 重复链接自动去重 |
| `id_regex` | string | 空 | 从链接提取章节 ID; 缺省时取路径最后一段去扩展名 |
| `id_group` | integer | `1` | 捕获组序号, 必须 >= 0; 越界时回退到整段匹配 |
| `volume_name` | string | `正文` | 归入的分卷名 |
| `is_desc` | boolean | `false` | 站点目录"最新在前"时设为 `true`: 合并全部分页后把**整条**章节列表反转一次 (不是逐页反转) |
| `container` | string | 空 | 仅配合 `preprocess`: 把该节点元素子节点的序列化结果交给重写链 (裸文本子节点不包含, 见下方) |
| `preprocess` | step[] | `[]` | 解析目录前对 HTML 的重写链, 见下方 |
<!-- /rule-dsl:keys:chapters -->

---

### `chapter`: 正文

<!-- rule-dsl:keys:chapter -->
| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `url` | string | — | 必填, 正文页模板, 支持 `{base_url}` / `{book_id}` / `{chapter_id}` |
| `content` | field | `{"selector": "body"}` | 正文提取 |
| `title` | field | 空 | 章节标题 (可选) |
| `replace` | `[string, string][]` | `[]` | 逐条字面替换, 按数组顺序执行 |
| `drop` | string[] | `[]` | 正则数组; 命中任一正则的**整行**删除 |
| `pages` | object \| string | 空 | 章内分页规则 |
| `headers` | object | `{}` | 本节请求头, 值支持模板变量 |
| `format` | string | 继承顶层 | 本节响应格式 |
| `container` | string | 空 | 仅配合 `preprocess`: 把该节点元素子节点的序列化结果交给重写链 (裸文本子节点不包含, 见下方) |
| `preprocess` | step[] | `[]` | 解析正文前对 HTML 的重写链, 见下方 |
<!-- /rule-dsl:keys:chapter -->

正文处理顺序固定为: 逐页提取 → `"\n"` 拼接 → `chapter.replace` → 按行 `drop` → 首尾 `strip`。空结果返回 `None`, 不会写出空章节。

---

### `pages`: 分页

<!-- rule-dsl:keys:pages -->
| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `url` | string | — | 必填, 页码模板, 支持 `{page}` 以及所在段的其它变量 |
| `start` | integer | `2` | 起始页码 (第 1 页由主链接抓取) |
| `max_pages` | integer | 空 | 指定后固定抓取 `start` ~ `max_pages`, 不再依赖"下一页链接是否存在" |
| `stop_when` | string | 空 | 正则 (开启 `re.S`); 命中当前页响应体时停止翻页 (当前页保留) |
| `loop_guard` | boolean | `true` | 重复 URL / 相同响应体时停止翻页, 防止自指链接死循环 |
<!-- /rule-dsl:keys:pages -->

未设置 `max_pages` 时, 引擎检查当前页响应中是否仍出现下一页 URL (完整后缀或文件名), 出现则继续; 额外页数硬上限为 50。`stop_when` 先检查**入口页** (命中则一个额外页都不抓), 之后每一页抓取后与 `loop_guard` 一并判定, 先触发者生效。`loop_guard` 命中重复 URL 或相同响应体摘要时停止; 设为 `false` 可恢复无保护的启发式。

`max_pages` 小于 `start` 时不会报错, 只是一个额外页都不抓。JSON 接口通常不在响应体里回链下一页, 因此**必须**用 `max_pages` 指定范围 (可再用 `stop_when` 命中空列表来提前结束); 只写 `stop_when` 而没有 `max_pages` 时, 由于当前页不含下一页 URL, 第一页额外请求都不会发出。目录分页结果按章节链接去重合并, 正文分页结果按顺序拼接。

---

### `field`: 字段提取

`field` 可以是字符串 (等价于 `{"selector": "..."}`), 也可以是对象:

<!-- rule-dsl:keys:field -->
| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `selector` | string | 空 | 选择器; 留空表示直接使用当前节点 |
| `attr` | string | `text` | 取值方式, 见下; 空字符串是非法值 |
| `regex` | string | 空 | 对取到的值做一次正则**搜索** (开启 `re.S`, `.` 可跨行) |
| `group` | integer | `1` | 正则捕获组序号, 必须 >= 0; 越界时回退到整段匹配 |
| `multi` | boolean | `false` | 收集全部匹配; 引擎目前只在 `book.fields.tags` 上生效, 其它位置仍只取第一个非空匹配 |
| `replace` | `[string, string][]` | `[]` | 正则处理后逐条字面替换 |
| `default` | string | `""` | 所有选择器与回退项都没有结果时的兜底值 |
| `steps` | step[] | `[]` | 后处理链, 在 `regex` / `replace` 之后执行, 见下方 |
| `fallback` | (string \| field)[] | `[]` | 选择器回退链, 主选择器为空时依次尝试, 见下方 |
<!-- /rule-dsl:keys:field -->

执行顺序: `selector` 选中节点 → `attr` 取值 → `regex` / `group` → `replace` → `steps` → 首尾 `strip`。`steps` 只在当前值为非空时执行, 避免常量表达式给"选择器没有匹配"凭空造出内容。`default` 只在主字段与全部回退都失败后使用。

`attr` 的取值:

| 值 | HTML 模式 | JSON 模式 |
| --- | --- | --- |
| `text` | 后代文本去空白后拼接 | 标量取自身 (数字 / 布尔转字符串); 对象序列化为 JSON 文本; 数组把各元素的文本用空串拼接 |
| `inner_text` | 后代文本按行拼接, 保留段落 | 标量取自身; 对象同样序列化为 JSON 文本; 数组按 `\n` 拼接 (仅当整个匹配结果就是数组时, 见下方注意) |
| `html` | 节点的 inner HTML | 对象序列化为 JSON 文本; 数组把各元素的文本用空串拼接 |
| 其它字符串 | 读取同名 HTML 属性, 如 `href` / `src` / `content` | 读取对象的同名键; 数组序列化为 JSON 文本 |

> **注意 (JSON 数组)**: 路径**末段**指向数组时, 引擎会先把数组展开成多个节点 (见下方 `selector` 语法), 每个节点再套用 `attr`。因此 `data.content` 若是一个段落数组, 只会取到第一个非空元素, 而不是把它们拼起来。需要整段内容时, 让接口返回字符串 (常见做法), 或把字段指向包含该数组的对象再配合其它字段处理。

`selector` 语法:

- **HTML**: 以 `/`、`./`、`(` 开头按 XPath 解析, 其余按 CSS 解析;
- **JSON**: 点号路径。`data.list` 取数组并自动展开; `[0]` 显式下标 (不再展开); `[*]` 展开全部元素; `items.title` 对数组元素隐式逐个取 `title`; 前导 `$` 与 `.` 可省略。数组展开发生在**末段**, 因此末段为数组时字段只会看到展开后的单个元素 (见上方注意)。

---

### `step`: 后处理链与 HTML 重写

`steps` (字段后处理) 与 `preprocess` (解析前 HTML 重写) 使用同一种条目:

<!-- rule-dsl:keys:step -->
| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `lang` | string | — | 必填, `js` 或 `py` |
| `code` | string | 空 | `lang=js` 时必填, 一段 mini-js 程序 |
| `name` | string | 空 | `lang=py` 时必填, 白名单函数名 |
<!-- /rule-dsl:keys:step -->

#### `py` 步骤

`name` 只能取自引擎的显式白名单 (纯 `str -> str`, 无副作用):

`strip_html` / `normalize_space` / `dedupe_lines` / `base64_decode` / `url_decode` / `url_encode` / `html_unescape` / `strip` / `lower` / `upper` / `reverse`

规则文件**不能内联 Python 代码**: 书源是外部输入, 允许携带可执行 Python 等于开放远程代码执行。未知名字在 `parse_rule` 阶段直接报错并列出可用名字。`py` 步骤只能引用上表名字, **不能**使用下面 `js` 步骤专用的多参 helper (那属于另一份白名单)。

#### `js` 步骤

`code` 由 `novel_downloader.libs.mini_js` 执行。这是一个刻意很小的子集:

- **支持**: `let` 声明、`function` 定义与调用、函数体内的 `return`、表达式语句、算术 (`+ - * /`)、三元表达式、对象 / 数组字面量, 以及字典 / 列表的成员访问 (`obj.key`、`arr[0]`);
- **不支持**:
  - `if`、`for` / `while` 等循环、正则字面量 (`/re/`) —— 在**解析阶段**报错;
  - 字符串原型方法 (`.replace` / `.split` / `.trim` / `.toUpperCase`) —— 能通过解析, 在**运行阶段**报错 (`Member access on non-object`); 字符串也没有 `.length`;
  - `new` 与任何全局构造器 / 全局函数 (`parseInt` 等) —— 运行阶段报 `NameError`;
  - 顶层的裸 `return` —— 只能在函数体内使用;
- 当前字符串绑定为 `input` (别名 `result`);
- 上述 `py` 白名单函数会以可调用函数的形式注入, 因此 `normalize_space(input)` 合法, 而 `input.trim()` 非法。

因为 mini-js 没有字符串方法, 改写 HTML 时无法用 `.replace`。为此额外注入两个**仅 `js` 步骤可用**的多参 helper (它们不在 `py` 步骤白名单里, `{"lang": "py", "name": "replace_all"}` 会在加载阶段报错):

| helper | 说明 |
| --- | --- |
| `replace_all(text, old, new)` | 字面量替换全部出现 |
| `regex_replace(text, pattern, repl)` | 按 Python `re.sub` 语义替换; 替换串里用 `\1` 而不是 `$1` |

```json
"preprocess": [
  {"lang": "js", "code": "regex_replace(input, '<div class=\"ad\">.*?</div>', '')"}
]
```

**正则的反斜杠要经过三层**: 规则 JSON 字符串 → mini-js 字符串字面量 → Python `re`。因此想表达 `\d+`, 在 `.json` 文件里要写**四个反斜杠**:

```json
{"lang": "js", "code": "regex_replace(input, '\\\\d+', '#')"}
```

JSON 解码后是 `regex_replace(input, '\\d+', '#')`, mini-js 再把 `\\` 解成 `\`, 最终正则收到 `\d+`。只写两个反斜杠会变成 `\d` → JS 字符串里退化为 `d`, 正则静默匹配不到 (不会报错), 这是最容易踩的坑。

不支持的写法会在加载阶段或运行阶段抛出带规则位置的 `RuleError`, 不会"原样返回"。

```json
"title": {
  "selector": "h1",
  "steps": [
    {"lang": "js", "code": "normalize_space(strip_html(input))"},
    {"lang": "py", "name": "strip"}
  ]
}
```

**解决的具体站点问题**: 标题同时带 HTML 片段、全角空格和 "最新章节" 之类的重复文案; 正文里有重复的防盗行; 需要按站点做 base64 / URL 解码。

#### `preprocess` 与 `container`

`book.chapters.preprocess` 与 `chapter.preprocess` 在**解析之前**重写 HTML:

```json
"chapters": {
  "item": "dd a",
  "container": "div#catalog",
  "preprocess": [
    {"lang": "py", "name": "html_unescape"}
  ]
}
```

- `container` 留空时, 重写链的输入是整页原始响应; 设置后取该节点的 **inner HTML**;
- 这里的 inner HTML 是**元素子节点**的序列化结果, 直接挂在容器下的裸文本节点不会进入重写链。若整个目录就是一段被转义的纯文本 (常见于 `&lt;dd&gt;&lt;a ...&gt;` 直接贴在 `div#catalog` 里), 用 `container` 会得到空文档并报错——此时应留空 `container`, 改写整页;
- 若 `container` 匹配不到任何节点, 直接报错, 而不是静默产出零章节;
- 重写结果必须是非空、可解析的 HTML, 否则报错;
- 链按数组顺序执行, 上一步的输出是下一步的输入;
- 只在 `format: "html"` 下可用; 在 JSON 段使用 `preprocess` 会直接报错;
- 重写后的文档是**新的根**, 所有选择器都在改写后的树上执行。使用 `container` 后该容器本身已不在树中, 因此 `item` / `content` 不能再以它为前缀 (`container: "div#catalog"` 时 `item` 写 `dd a`, 不能写 `div#catalog dd a`);
- `book.chapters.preprocess` 只作用于**目录解析**, `book.fields` 的详情字段仍从原始页面提取; `chapter.preprocess` 作用于**整个正文段解析**, 包括 `chapter.title` 与 `chapter.content`, 因此把 `h1` 放在 `container` 之外会导致标题取不到。

**解决的具体站点问题**: 目录被双层转义 (`&lt;a href...&gt;`) 需要先还原; 站点把章节列表塞进 `display:none` 的诱饵块; 相对链接需要统一成绝对地址。

#### `fallback` 回退链

```json
"book_name": {
  "selector": "meta[property='og:novel:book_name']",
  "attr": "content",
  "fallback": [
    {"selector": "h1.book-name", "attr": "text"},
    {"selector": "//h1[@class='book-name']", "attr": "text"}
  ]
}
```

- 字符串项**继承**主字段的 `attr` / `regex` / `group` / `replace` / `steps`, 只替换 `selector`;
- 对象项是一个独立的字段 (可自带 `attr` / `regex` / `steps` 等);
- 主选择器整条链为空时才尝试回退, 第一个非空结果胜出;
- 回退项自己的 `default` 不生效, 主字段的 `default` 是最后兜底。

> **继承 `attr` 的坑**: 主字段是 `meta` + `attr: "content"` 时, 字符串回退项也会用 `attr: "content"` 去读 `<h1>` 的 `content` 属性——没有这个属性, 结果是空串, 回退**看起来没生效**。`meta` 与 `h1` 混用时, 回退项必须写成对象并显式给 `attr: "text"` (如上)。

**解决的具体站点问题**: 站点 A/B 测试两套详情页模板 (一套 `meta` 标签、一套 `h1`); 主站与镜像站字段布局不同; 老页面没有简介块。

---

### `limits`: 规则级并发与重试

```json
"limits": {
  "concurrency": 2,
  "min_interval": 1.0,
  "max_interval": 2.5,
  "max_attempts": 3,
  "retry_min_interval": 5,
  "retry_max_interval": 20
}
```

<!-- rule-dsl:keys:limits -->
| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `concurrency` | integer | 空 | 该源同时进行的请求数, 必须 >= 1; `0` 是非法值 |
| `min_interval` | number | 空 | 相邻请求间隔的下界, **单位: 秒** |
| `max_interval` | number | 空 | 相邻请求间隔的上界, **单位: 秒** |
| `max_attempts` | integer | 空 | 单次请求的最大尝试次数 (含首次), 必须 >= 1 |
| `retry_min_interval` | number | 空 | 重试退避的下界, **单位: 秒** |
| `retry_max_interval` | number | 空 | 重试退避的上界, **单位: 秒** |
<!-- /rule-dsl:keys:limits -->

约束与映射:

- `min_interval <= max_interval`, `retry_min_interval <= retry_max_interval`; 反向区间在解析时报错;
- 未设置的字段保持全局配置不变; 合并是**不可变**的 (`dataclasses.replace` 生成新配置), 不会修改共享的全局对象;
- `concurrency` 映射到 `max_connections`;
- `min_interval` / `max_interval` 取中点后映射到 `request_interval`;
- `max_attempts` 映射到 `retry_times = max_attempts - 1` (后者只计首次失败后的重试次数);
- `retry_min_interval` / `retry_max_interval` 取中点后映射到 `backoff_factor`。

**解决的具体站点问题**: 某些源高频请求直接 429/403, 需要把该源降到 1 并发、2 秒间隔, 但不拖慢其它源; 小站需要串行抓取才不封 IP; 个别源偶发 502, 需要更多重试与更长退避。

---

### `search.single_result_fallback`: 单结果回退

默认 `false`。开启后, 当 `item` 选择器**没有匹配到任何条目**时, 引擎会把整个响应当作一条搜索结果, 但仅在满足以下任一证据时生效:

- 最终响应 URL 与请求 URL 不同 (说明发生了重定向); 或
- 页面上能取到非空的 `book.fields.book_name` (或回退到 `search.fields.title`)。

结果链接使用**最终响应 URL**; 元数据优先取 `book.fields` 的对应字段, 缺失时回退到 `search.fields`:

| 输出 | 优先 (`book.fields`) | 回退 (`search.fields`) |
| --- | --- | --- |
| 标题 | `book_name` | `title` |
| 作者 | `author` | `author` |
| 封面 | `cover_url` | `cover` |
| 更新时间 | `update_time` | `update` |
| 字数 | `word_count` | `word` |

`latest` 在单结果回退中始终为空。

```json
"search": {
  "url": "https://example.test/search?q={keyword}",
  "item": "ul.result-list > li",
  "single_result_fallback": true,
  "fields": {
    "title": {"selector": "h1.book-name", "attr": "text"},
    "url": {"selector": "h1.book-name a", "attr": "href"}
  }
}
```

**解决的具体站点问题**: 精确书名搜索时站点 302 到详情页, 响应里没有结果列表; 只有一个匹配时渲染单张卡片而没有列表容器。它只在"零结果"时兜底, 正常匹配到条目时不会额外追加, 以免重复。

---

### `resolve`: 链接解析

<!-- rule-dsl:keys:resolve -->
| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `book` | string | 空 | 从 URL path 提取 `book_id` 的正则, 第 1 个捕获组为 ID |
| `chapter` | string | 空 | 从 URL path 提取 `book_id` / `chapter_id` 的正则, 两个捕获组依次对应 |
<!-- /rule-dsl:keys:resolve -->

`chapter` 优先于 `book` 匹配; 两个都留空时, 该规则的 `hosts` 命中也不会解析出站内 ID。

---

### 模板变量

| 变量 | 可用位置 | 说明 |
| --- | --- | --- |
| `{base_url}` | book / chapter (含各自的 `pages.url`) 的 URL、`search.url`、book / chapter 的 `headers` 值 | 站点根地址 (已去末尾 `/`) |
| `{book_id}` | book / chapter (含各自的 `pages.url`) 的 URL、book / chapter 的 `headers` 值 | 书籍 ID |
| `{chapter_id}` | chapter (含 `pages.url`) 的 URL、chapter 的 `headers` 值 | 章节 ID |
| `{page}` | `pages.url`、`search.url`、`search` 的 `data` / `json_body` 值 | 页码; 搜索时固定为 `1` |
| `{keyword}` | `search.url`、`search` 的 `data` / `json_body` 值 | 关键词, 已 URL 编码 |
| `{raw_keyword}` | `search.url`、`search` 的 `data` / `json_body` 值 | 关键词原样 |

替换是单遍的: 插入的值不会被再次扫描, 因此页面数据里出现的 `{book_id}` 不会被二次展开。未识别的 `{...}` 原样保留。

注意两处边界:

- **顶层的 `headers` 与 `search.headers` 不做模板替换**, 变量只在 book / chapter 段的 `headers` 里生效;
- `search` 的 `data` / `json_body` 只展开 `{keyword}` / `{raw_keyword}` / `{page}`, 其中出现 `{base_url}` 会原样留给站点; 需要站点根地址时直接写进 `search.url` 或 `data` 的固定值。

---

### 单位与取值约定

- **所有间隔、退避类字段一律使用秒 (number)**, 不使用毫秒。字段名里不带单位时默认就是秒 (`1.5` 表示 1.5 秒)。这条约定来自一次真实事故: 模板注释写"秒"、代码按"毫秒"解释, 实际间隔只有预期的千分之一。
- **`concurrency` 必须是 >= 1 的整数。`0` 不是"无限", 也不是"串行", 而是非法值**; 零许可的信号量会永久阻塞。想要串行请写 `1`。
- `max_attempts >= 1` 与 `concurrency >= 1` 在解析期强制; 所有 `group` / `id_group` / `book_id_group` 必须 >= 0, 同样在解析期强制。
- `max_pages` **没有**解析期校验; 小于 `start` 时只是不抓任何额外页, 不报错。
- `priority` 数值越大越靠前; 搜索结果内部按顺序加 `0`、`1`、`2`…, 用于同分时的稳定排序。
- `encoding` 不做解析期校验; 必须是 Python 能识别的编码名, 下载时按该编码解码响应, 写错会在解码阶段失败。

---

### 不要这样做

#### 不要使用 `selector@js:code` 这类字符串 DSL

错误写法 (不要模仿):

```json
"content": {"selector": "//div[@class='content']@js:code"}
```

正确写法:

```json
"content": {
  "selector": "//div[@class='content']",
  "steps": [{"lang": "py", "name": "normalize_space"}]
}
```

为什么 `@` 方案不可行:

1. **`@` 在 XPath 里是属性谓词的一部分。** `//div[@class='content']` 是合法 XPath; 若按第一个 `@` 切分选择器与后处理, 会得到 `//div[` 与 `class='content']...`, XPath 解析直接失败。`//a[@href]`、`//img[@data-src]`、`(//div[@id='x'])[1]` 都会被截断。
2. **CSS 与 URL 里也可能出现 `@`。** 属性选择器可以匹配含 `@` 的值 (`a[href*='@']`), URL 常带 `@` (邮件、CDN 路径), mini-js 代码里也常见 `/@/`。一个没有转义规则的字符串分隔符无法安全表达这些内容。
3. **多段链式写法没有 schema。** `selector@js:a;@py:b` 只能靠约定解析, 拼错、漏写、多写都不会在 `parse_rule` 阶段被发现, 也不会进入本文与测试的一致性检查。
4. **显式数组可以被校验。** `steps` / `fallback` / `preprocess` 是结构化数据: 类型、长度、`lang` 取值都能在加载期验证, 与项目"未知键即报错"的策略一致。

#### 不要在 `steps` 里写完整 JavaScript

`js` 步骤运行在 mini-js 上, 不是完整的 V8/Node。`if`、循环、正则字面量、`.replace` / `.split` / `.trim` 都不支持。需要这些能力时, 优先使用白名单里的 `py` 步骤; 仍不够时, 应当为该站点写 Python 插件, 而不是在规则里堆砌不受支持的代码。

#### 不要用 `preprocess` 代替正文清洗

`preprocess` 的输入是整页 HTML (或 `container` 的内部 HTML), 在解析前执行。用它删除正文内容会同时删掉所有段落, 且无法被导出阶段的处理器恢复。正文清洗请用 `chapter.replace` (字面替换)、`chapter.drop` (按行正则删除)、`chapter.content.steps` (后处理链)。

#### 不要用 `py` 步骤执行任意代码

`{"lang": "py", "name": "..."}` 只引用引擎白名单中的函数。规则文件是外部输入, 允许其携带可执行 Python 会把"下载书源"变成"远程代码执行"。

#### 不要混淆单位与边界值

- `"min_interval": 500` 表示 500 秒, 不是 500 毫秒;
- `"concurrency": 0` 是非法值, 不是"自动"或"无限";
- 不要用 `max_pages` 表示"总页数上限"; 它表示"最后一个要抓的页码", 应当 `>= start` (写反了不会报错, 只是不抓任何额外页)。

#### 不要复制第三方书源规则

部分同类的开源项目采用 AGPL-3.0, 其规则 JSON 属于该项目的受版权保护内容。可以参考规则**结构**与**字段设计**, 但站点选择器与规则文档必须自己编写, 并使用本项目自己的字段名与校验规则。
