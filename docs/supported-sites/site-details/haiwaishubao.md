---
title: 海外书包
tags:
  - 简体中文
  - 转载站
  - 成人向
  - NSFW

---



## 基本信息

* 标识符: `haiwaishubao`
* 主页: https://www.haiwaishubao.com/
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
https://www.haiwaishubao.com/book/102659/
```

* Book ID: `102659`

### TOC URL

URL:

```
https://www.haiwaishubao.com/index/102659/
```

* Book ID: `102659`

### Chapter URL

URL:

```
https://www.haiwaishubao.com/book/102659/5335635.html
```

* Book ID: `102659`
* Chapter ID: `5335635`

---

## 适配器注册状态

* 适配器位于 `plugins/sites/haiwaishubao/`（`fetcher.py` / `parser.py` / `searcher.py` 齐全），但尚未在 `src/novel_downloader/apps/constants.py` 的 `DOWNLOAD_SUPPORT_SITES` / `SEARCH_SUPPORT_SITES` 中注册。
* 因此当前无法通过 CLI / Web / API 选择该站点，也不参与聚合搜索。
