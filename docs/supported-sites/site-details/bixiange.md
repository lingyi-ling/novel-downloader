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
https://m.bixiange.me/wxxz/20921/index/1.html
```

* Book ID: `wxxz-20921`
* Chapter ID: `1`

---

## 其它说明

* 该站点直接提供 txt 下载。

## 适配器注册状态

* 适配器位于 `plugins/sites/bixiange/`（`fetcher.py` / `parser.py` / `searcher.py` 齐全），并在 `DOWNLOAD_SUPPORT_SITES` 中注册。
* **旧域名 `m.bixiange.me` 已无 DNS 解析（NXDOMAIN）**，现有适配器无法连接，因此未注册搜索。
* 站点已迁移至 `m.bixiange.top`（备用 `www.bixiange.xyz`），实测详情页与章节页仍可被现有解析器解析；待适配器更新域名后再验证搜索。
