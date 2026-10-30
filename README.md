# novel-downloader

[中文](./README.md) | [English](./README.en.md)

异步、可扩展的小说下载与处理工具包, 同时内置多平台音乐下载。

支持断点续爬、下载队列与进度、多格式导出、文本处理流水线, 提供 CLI (`novel-cli`)、本地 REST API (`novel-api`)、桌面客户端 (`novel-desktop`) 与 Android 客户端四种使用方式。

**运行要求**: Python 3.11+ (开发环境: Python 3.13)

---

## 功能特性

- **多站点搜索:** 覆盖 57 个搜索站点与 83 个下载站点, 支持聚合搜索、排序与结果筛选
- **下载队列与进度:** 支持断点续爬、队列调度与实时进度显示
- **多格式导出:** TXT / EPUB / HTML
- **历史导出:** 从已下载的书目中随时重新导出
- **追更与自检:** 内置追更列表, 自动检查新章节并可选自动下载; `doctor` 环境自检与 `storage` 磁盘占用统计
- **异步与高性能下载**
- **可插拔 HTTP 后端:** `aiohttp` / `httpx` / `curl_cffi`
- **文本处理流水线:** 去广告、繁简转换、自动翻译等
- **图片章节与混淆章节支持 (可选)**
- **多平台音乐下载:** 支持 25 个音乐平台的搜索与下载
- **插件系统:** 可扩展站点解析器、导出器、Pipeline 等
- **规则书源 (Book Source):** 新增站点只需写 JSON 规则，无需编写 Python 代码
- **CLI + 本地 REST API + 桌面客户端:** `novel-cli`、`novel-api`、`novel-desktop` 共享同一服务层
- **Android 客户端:** 原生 Kotlin/Compose App（`android/`），通过本地 API 连接电脑端

---

## 安装与更新

使用 `pip` 安装最新稳定版本:

```bash
pip install -U novel-downloader
```

如需启用本地 REST API:

```bash
pip install "novel-downloader[api]"
```

如需启用音乐下载:

```bash
pip install "novel-downloader[music]"
```

如需启用其它可选功能 (本地 API、音乐、图片转文字、额外后端等), 请参见: [安装指南](./docs/guide/installation.md)

---

## 快速开始 (CLI)

```bash
# 下载一本小说
novel-cli download https://www.example.com/book/123/

# 使用站点 + 书籍 ID
novel-cli download --site n23qb 12282

# 搜索并下载音乐
novel-cli music search "关键词" --platform netease
novel-cli music download "关键词" --pick 1
```

更多示例见: [CLI 使用示例](./docs/guide/cli-examples.md)

## 本地 REST API

安装 `api` 可选依赖后, 使用 `novel-api` 启动仅监听本机的 REST 服务 (默认 `127.0.0.1:8081`), 供第三方程序调用搜索、任务队列、历史导出与音乐下载:

```bash
novel-api
```

端点、鉴权与请求示例见: [本地 REST API](docs/guide/api.md)

## 桌面客户端

```bash
novel-desktop
```

提供搜索、下载队列、历史导出与音乐下载的图形界面。

## 规则书源 (无需写代码即可新增站点)

除内置的 Python 站点插件外，本项目支持用一份 JSON 规则描述一个书源：
搜索、书籍详情、目录、正文、分页与 URL 解析全部由规则驱动，新增站点无需改动代码。

完整字段说明、加载顺序与已知限制见: [规则书源文档](./docs/guide/book-sources.md)

规则文件会从以下位置自动加载（后加载者覆盖前者）:

| 位置 | 说明 |
| --- | --- |
| `novel_downloader/plugins/rule/builtin/*.json` | 随项目内置的书源 |
| `./rules/*.json` | 运行目录下的自定义书源 |
| `$NOVEL_RULES_DIR/*.json` | 环境变量指定的目录 |
| `<用户配置目录>/novel_downloader/rules/*.json` | 每个用户的私有书源 |

```json
{
  "site_key": "example",
  "site_name": "示例书源",
  "base_url": "https://www.example.com",
  "encoding": "utf-8",
  "hosts": ["www.example.com"],
  "search": {
    "url": "https://www.example.com/search.php?q={keyword}",
    "item": "div.result-list dl",
    "fields": {
      "title": {"selector": "dd h3 a", "attr": "text"},
      "url":   {"selector": "dt a", "attr": "href"},
      "author":{"regex": "作者：\\s*(\\S+)", "group": 1}
    }
  },
  "book": {
    "url": "{base_url}/book/{book_id}/",
    "pages": {"url": "{base_url}/book/{book_id}/index_{page}.html", "start": 2},
    "fields": {
      "book_name": {"selector": "meta[property='og:novel:book_name']", "attr": "content"},
      "summary":   {"selector": "meta[property='og:description']", "attr": "content"}
    },
    "chapters": {
      "item": "div.listmain a",
      "title": {"attr": "text"},
      "url":   {"attr": "href"},
      "id_regex": "/(\\d+)\\.html"
    }
  },
  "chapter": {
    "url": "{base_url}/book/{book_id}/{chapter_id}.html",
    "content": {"selector": "#content", "attr": "inner_text"},
    "drop": ["^第\\(\\d+/\\d+\\)页$"]
  },
  "resolve": {
    "book": "^/book/(\\d+)/?$",
    "chapter": "^/book/(\\d+)/(\\d+)\\.html$"
  }
}
```

要点:

* `selector` 支持 CSS 选择器与 XPath（以 `/` 或 `(` 开头视为 XPath）。
* `attr` 取值: `text`（拼接文本）/ `inner_text`（按行拼接）/ `html` / 任意属性名（如 `href`、`src`、`content`）。
* `{keyword}` 会被 URL 编码后填入，`{page}`、`{book_id}`、`{chapter_id}`、`{base_url}` 同理。
* `hosts` + `resolve` 用于支持「直接粘贴链接下载」；已有内置插件优先于规则书源。
* `pages` 用于目录/正文分页：当下一页链接仍出现在当前页 HTML 中时继续抓取。

查看与管理书源:

```bash
# 列出所有规则书源（--check 会额外校验能否成功注册）
novel-cli sources list --check

# 校验单个规则文件
novel-cli sources check ./rules/example.json

# 使用规则书源搜索 / 下载
novel-cli search "斗破苍穹" -s biquge5r
novel-cli download --site biquge5r 0_143 --start 155832 --end 155835
```

内置的 `biquge5r.json` 是一个完整可用的示例书源，可作为编写新规则的起点。

## 编程接口 (Programmatic API)

```python
import asyncio
from novel_downloader.plugins import registrar
from novel_downloader.schemas import BookConfig, ClientConfig

async def main() -> None:
    site = "n23qb"

    # 指定书籍 ID
    book = BookConfig(book_id="12282")

    # 创建客户端
    cfg = ClientConfig(request_interval=0.5)
    client = registrar.get_client(site, cfg)

    # 在异步上下文中执行下载
    async with client:
        await client.download_book(book)

    # 下载完成后执行导出操作
    client.export_book(book, formats=["txt", "epub"])

if __name__ == "__main__":
    asyncio.run(main())
```

更多示例见: [API 示例](./docs/reference/api-examples.md)

---

## 贡献与开发

```bash
git clone https://github.com/lingyi-ling/novel-downloader.git
cd novel-downloader

# 可选: 为多语言支持编译翻译文件
# pip install babel
# pybabel compile -d src/novel_downloader/locales

pip install .
# 或安装带可选功能:
# pip install .[all]
# pip install -e .[dev,all]
```

欢迎提交 Issue / PR。

---

## 注意事项

* **站点结构变更**: 若目标站点页面结构更新或章节抓取异常, 欢迎提 Issue 或提交 PR
* **登录支持范围**: 登录功能受站点策略与接口限制, 部分场景需要手动配置 Cookie 或进行账号绑定
* **请求频率**: 请合理设置抓取间隔, 避免触发风控或导致 IP 限制
* **遇到报错**: 先查阅 [故障排查](docs/guide/troubleshooting.md); 代码结构见 [架构说明](docs/guide/architecture.md)

---

## 项目说明

* 本项目仅供学习和研究使用, **不得**用于任何商业或违法用途
* 请遵守目标网站的 `robots.txt` 及相关法律法规
* 使用本项目产生的任何法律责任由使用者自行承担, 作者不承担相关责任
