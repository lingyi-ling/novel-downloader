# 保留的桌面版音乐下载器（legacy）

这个目录保存合并进主项目之前的 customtkinter 桌面界面（网易云下载器）。
现在主入口是 Web UI 的「音乐」页面（`/music`），桌面版仅作为备份保留，
不再随主项目做质量门禁（ruff / mypy 不覆盖本目录）。

依赖（除主项目外）：

```bash
pip install -e ".[music-desktop]"
```

运行：

```bash
python legacy/music_desktop/main.py          # 打开窗口
python legacy/music_desktop/main.py --check  # 自检（结果写入数据目录）
```

运行数据（登录会话、设置、浏览器 profile）与 Web 版共用
`novel_downloader.apps.music.paths.music_data_dir()` 指向的目录。
