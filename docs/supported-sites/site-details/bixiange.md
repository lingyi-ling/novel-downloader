---
title: 笔仙阁
tags:
  - 简体中文
  - 转载站

---

笔仙阁提供免费的手机小说,小说在线阅读,txt小说下载,不放任何弹窗。

## 基本信息

* 标识符: `bixiange`
* 主页: https://m.bixiange.top/
* 备用域名:
    * https://www.bixiange.top/
    * https://m.bixiange.me/
* 语言: 简体中文
* 站点状态: :orange_circle: Unstable
* 支持分卷: :material-close: 否
* 支持插图: :material-close: 否
* 支持登录: :material-close: 否
* 支持搜索: :material-check: 是

---

## URL 示例

### Book URL

URL:

```
https://m.bixiange.top/wxxz/20921/
```

* Book ID: `wxxz-20921`

### Chapter URL

URL:

```
https://m.bixiange.top/wxxz/20921/index/1.html
```

* Book ID: `wxxz-20921`
* Chapter ID: `1`

---

## 其它说明

* 该站点直接提供 txt 下载。

## 适配器注册状态

* 适配器位于 `plugins/sites/bixiange/`（`fetcher.py` / `parser.py` / `searcher.py` 齐全），已在 `DOWNLOAD_SUPPORT_SITES` / `SEARCH_SUPPORT_SITES` 中注册。
* 旧域名 `m.bixiange.me` 已无 DNS 解析（NXDOMAIN）；适配器已切换到 `m.bixiange.top`，搜索器与下载器均使用新域名。
* 搜索关键词按 GBK 编码提交；GBK 字节数超过 20 的关键词会被站点当作无结果，下载器会在 GBK 字符边界截断到 20 字节以内再查询。
* 站点对同一 Cookie 会话内的两次搜索有 15 秒间隔限制；聚合搜索每次新建会话，正常使用不会触发。
