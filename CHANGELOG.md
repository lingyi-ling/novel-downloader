## v1.0.0 (2026-10-16)

首个发布版本。

### Feat

- **rules**: 规则书源子系统 —— 用一份 JSON 描述站点，搜索、详情、目录、正文、分页与链接解析全部由规则驱动，新增站点无需编写 Python 代码
- **rules**: 内置 `biquge5r` 规则源，并提供覆盖全部字段的规则模板
- **rules**: `book` / `chapter` 段可单独设置请求头，且请求头值支持 `{base_url}` / `{book_id}` / `{chapter_id}` 模板变量
- **cli**: 新增 `sources list` / `sources check` 用于查看与校验规则书源

### Fix

- **rules**: 修复分页页被重复收集导致分页章节正文重复的问题
- **rules**: 空页或退化页面统一报 `RuleError`，不再泄漏底层解析异常；正文解析会跳过这类页面
- **cli**: `prompt_choice` 捕获 EOF，管道或脚本运行时不再崩溃

### Docs

- 补充规则书源文档、示例规则模板与导航入口
