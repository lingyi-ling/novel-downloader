# Web UI 重写为 Claude (Anthropic) 风格 实施计划

## Context (背景与目标)

现有 Web/桌面 UI 是 NiceGUI + Quasar 默认样式：蓝色顶栏 (`bg-primary` 蓝底白字) + 默认 Material 卡片/投影，观感与项目定位不符。

用户已通过澄清确认：
1. **风格参照** = Anthropic / Claude 界面视觉语言：暖米白底、珊瑚橙强调色、衬线大标题、1px 细描边卡片、大留白、克制无投影。
2. **改动边界** = 完全重写（可重构页面模块与交互流程外观）。
3. **明暗双主题保留**（浅色=暖米白，深色=暖黑），切换按钮保留。

**硬性约束：现有功能必须 100% 保留，不新增运行时依赖。**

技术结论（已核实 NiceGUI 3.18.0 源码）：
- `ui.add_css(content, shared=True)` 会写入 `Client.shared_head_html`，对**所有页面**生效；可在模块导入期调用一次，无需逐页 `ui.colors`。
- Quasar 品牌色只是 `:root { --q-primary: … }` 这类 CSS 变量；暗色由 `body.body--dark` 类切换。
  => 纯 CSS 覆盖令牌即可完成全站换肤，两套主题天然支持。
- 页面骨架固定为 `q-layout > q-page-container > q-page > div.nicegui-content`；`.nicegui-card` 默认 `padding/gap = 1rem`。

## 视觉系统 (Design Tokens)

浅色 (`:root`)：

| 令牌 | 值 | 用途 |
| --- | --- | --- |
| `--nd-bg` | `#F5F4EF` | 页面底色（暖米白） |
| `--nd-surface` | `#FFFFFF` | 卡片 |
| `--nd-surface-2` | `#FAF9F5` | 悬停/次级块 |
| `--nd-border` | `#E4E1D7` | 1px 细描边 |
| `--nd-border-strong` | `#D6D2C4` | 输入聚焦描边 |
| `--nd-text` | `#1F1E1B` | 主文本 |
| `--nd-text-muted` | `#6B675E` | 次要文本 |
| `--nd-text-faint` | `#96918A` | 最弱文本 |
| `--nd-accent` | `#D97757` | 珊瑚橙主强调 |
| `--nd-accent-hover` | `#C4663F` | 悬停 |
| `--nd-accent-soft` | `rgba(217,119,87,.10)` | 选中/胶囊底色 |
| `--nd-positive/negative/warning/info` | `#3F7F5C` / `#B4472F` / `#A9762A` / `#5A7C8C` | 状态 |

深色 (`body.body--dark`)：
`--nd-bg #1B1A17` / `--nd-surface #23221F` / `--nd-surface-2 #2A2925` /
`--nd-border #3A3833` / `--nd-text #EDEBE6` / `--nd-text-muted #A8A399` /
`--nd-accent #E0895F`，状态色同系提亮。

同时映射 Quasar 令牌（两套主题各自）：`--q-primary=--nd-accent`、
`--q-secondary=--nd-text-muted`、`--q-accent/positive/negative/warning/info=--nd-*`、
`--q-dark=#22211E`、`--q-dark-page=--nd-bg`。这样现有 `color="primary"`
/ `ui.notify(type="warning")` 等无需逐个改写即自动变为暖色系。

字体：
- `--nd-font-sans`: `ui-sans-serif, -apple-system, "Segoe UI", Roboto, "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif`
- `--nd-font-serif`（大标题）: `ui-serif, "Iowan Old Style", Georgia, "Songti SC", "Noto Serif SC", serif`
- `--nd-font-mono`: `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`

形状/层次：卡片圆角 14px、控件 10px、胶囊 999px；边框 1px；**去投影**（仅悬停极淡柔光）；页面内容宽度上限 1120px、区块间距加大。

全局覆盖的 Quasar 组件（一次写好，所有页面复用）：
`body` 背景、`.q-header`、`.q-card`/`.nicegui-card`、`.q-btn`（去大写、圆角、聚焦环）、
`.q-btn-toggle`、`.q-field--outlined .q-field__control`（圆角/描边/聚焦珊瑚）、
`.q-select`、`.q-checkbox`、`.q-toggle`、`.q-chip`、`.q-linear-progress`、
`.q-pagination`、`.q-menu`/`.q-item`、`.q-separator`、`.q-dialog`、`a`。

页面级工具类（`theme.py` 内定义）：`.nd-page`、`.nd-hero`、`.nd-h1`、`.nd-h2`、
`.nd-muted`、`.nd-card`、`.nd-nav-item`、`.nd-btn-ghost`、`.nd-pill`（含
`--running/--queued/--done/--failed` 变体）、`.nd-cover`、`.nd-link`、
`.nd-skeleton`（暖色微光动画）。

## 实施步骤

### 1. 新增 `src/novel_downloader/apps/web/theme.py`（唯一新文件）
- 常量 `THEME_CSS: str`（**每行必须 < 88 字符**，E501 在字符串内同样生效）。
- `install() -> None`：用模块级 `_installed` 标志做幂等，内部
  `ui.add_css(THEME_CSS, shared=True)`。
- 不引入新依赖、不改 `pyproject.toml`。

### 2. 改写 `components/navigation.py`（保留 `navbar(active)` 签名）
- 入口先调用 `theme.install()`。
- 顶栏由 `bg-primary text-white` 改为透明/表面色 + 底部 1px 描边，粘性 + 轻微模糊。
- 左侧：衬线字标 `t("Novel Downloader")`；导航项改为胶囊文本（激活=accent-soft 底 + 珊瑚文字，未激活=muted）。
- 右侧：语言 `ui.select`（细描边紧凑样式，`shared` 逻辑不变）+ 圆形图标按钮切换主题。
- 保留 `_dark_state`、`switch_language`（`set_language` + `ui.navigate.reload()`）行为。

### 3. 改写各页面（**仅外观与结构，逻辑零改动**）
- `pages/search.py`：衬线标题（复用已有 `t("Search")`，不新增 msgid）；工具栏/结果行/骨架/分页样式化；结果行改 `.nd-card`；封面 `.nd-cover`；下载按钮小号主色。保留：设置菜单（站点勾选/全选/清空、per-site limit、timeout）、
  Search/Stop、`LOGIN_SITES` 登录按钮与子进程拉起、排序（key+order）、流式追加+重排、骨架、分页、`←/→` 翻页、断开清理。
- `pages/download.py`：衬线标题 + 分段切换（URL / Site+ID）样式化、输入框圆角、预览胶囊改 `.nd-pill`、Clear/Add 按钮。保留：blur 解析、预览可见性、Enter 提交、ID 框粘 URL 自动切模式、`loading` 防重复提交。
- `pages/progress.py`：衬线标题 + 分区标题（`.nd-h2`，复用 `t("Running / Queued")` 等既有文案）；任务卡改 `.nd-card`；状态标签由 Quasar `chip color=` 改为 `.nd-pill--<status>`（**状态集合与文案不变**）；进度条细珊瑚色；导出下载按钮 ghost 样式。保留 0.5s 定时刷新、取消、EA/汇总文案。
- `pages/history.py`：衬线标题、工具栏控件与文件行样式化（图标加暖色方块底、Download ghost 按钮）。保留类型过滤、排序字段/顺序、Refresh、计数文案、分页、`←/→`。
- `services/client_dialog.py`：登录弹窗改新样式（衬线标题、`.nd-card`、圆角输入、珊瑚提交 + ghost 取消）。**领取/续期/超时/断线清理逻辑一律不动。**

### 4. 文档同步（轻量）
- `docs/guide/web-examples.md` 与 `.en.md`：更新内嵌截图
  `docs/assets/images/NiceGUI_{search_santi,download_by_url,download_by_id,progress,history}.png`；
  顺手修正排序默认值描述（文中写 `Priority`，代码默认已是 `Quality`/优选）。
- 无新增用户可见字符串 ⇒ 不改 `.po`（如确需新增文案，跑 `python scripts/update_pot.py` →
  `update_po.py` → `compile_mo.py`）。

## 验证 (Verification)

1. 静态检查全绿：
   `ruff check src tests`、`ruff format --check src tests`、`mypy src`。
2. 单测全量：`pytest -q`（基线 4200 passed / 25 skipped；已知 `test_load_config_not_found` 已修）。
3. 端到端实测：以空闲端口启动 `novel-web --port <free>`，用浏览器子代理访问
   `/`、`/download`、`/progress`、`/history`，分别在**浅色 + 点击切换后的深色**下截图；
   核对无 console 报错、无布局溢出；实操一次搜索→加入队列→进度页可见→历史页可下载。
4. 收尾：清理临时探查产物；`ruff format --check` 复核（防编辑器自动重排）；
   按仓库规范提交并推送（提交信息走临时文件 + `git -c user.name=... -c user.email=...`）。

## 风险与规避

| 风险 | 规避 |
| --- | --- |
| CSS 字符串触犯 E501 | 逐行 < 88 字符，选择器可换行书写 |
| Quasar 内部 DOM 结构差异导致样式失效 | 只覆盖稳定类（`.q-field__control`、`.q-card`、`.q-btn`），不深入内部节点 |
| 改动影响既有功能 | 只改 `classes()/props()`（外观），不动事件、状态与异步逻辑；逐页对照功能清单 |
| 编辑器自动重排无关文件 | 提交前统一 `ruff format --check` 复核 |
