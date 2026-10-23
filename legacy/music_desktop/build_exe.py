"""一键打包成 exe：python build_exe.py

产物：dist/音乐下载器/音乐下载器.exe（onedir + 无控制台）

要点：
- providers 里的平台是 importlib 动态导入的，PyInstaller 静态分析不到，
  必须 --collect-submodules providers 才能把 vendored 的 91 个模块打进去
- customtkinter / fake_useragent / pywidevine / ytmusicapi 带数据文件，用 --collect-all
- 用户数据（session/settings/凭据）走 app_paths.app_root()，落在 exe 同目录，不进包
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "音乐下载器"

args = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--clean",
    "--windowed",              # GUI 程序，不要黑框控制台
    "--onedir",                # 目录模式：启动快、资源路径稳
    "--name", NAME,
    "--collect-submodules", "providers",
    "--collect-all", "customtkinter",
    "--collect-all", "fake_useragent",
    "--collect-all", "pywidevine",
    "--collect-all", "ytmusicapi",
    "--hidden-import", "app_paths",
    os.path.join(HERE, "main.py"),
]

print("运行：")
print("  " + " ".join(args))
print()
subprocess.run(args, cwd=HERE, check=True)
print("\n完成 ->", os.path.join(HERE, "dist", NAME, NAME + ".exe"))
