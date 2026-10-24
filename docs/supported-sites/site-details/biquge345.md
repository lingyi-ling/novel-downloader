---
title: 笔趣阁
tags:
  - 简体中文
  - 转载站
  - 笔趣阁

---



## 基本信息

* 标识符: `biquge345`
* 主页: https://www.biquge345.com/
* 语言: 简体中文
* 站点状态: :green_circle: Active
* 支持分卷: :material-close: 否
* 支持插图: :material-close: 否
* 支持登录: :material-help-circle: 部分支持
* 支持搜索: :material-close: 否

---

## URL 示例

### Book URL

URL:

```
https://www.biquge345.com/book/321301/
```

* Book ID: `321301`

### Chapter URL

URL:

```
https://www.biquge345.com/chapter/321301/37740966.html
```

* Book ID: `321301`
* Chapter ID: `37740966`

---

## 适配器注册状态

* 适配器位于 `plugins/sites/biquge345/`（`fetcher.py` / `parser.py` / `searcher.py` 齐全），但尚未在 `src/novel_downloader/apps/constants.py` 的 `DOWNLOAD_SUPPORT_SITES` / `SEARCH_SUPPORT_SITES` 中注册。
* 因此当前 CLI / Web / API 的站点列表中不会出现该站点，也不参与聚合搜索。
