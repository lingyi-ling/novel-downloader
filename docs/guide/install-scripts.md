## Windows 安装脚本

本章介绍如何使用仓库 `packaging/` 目录中的 PowerShell 脚本安装独立版
`novel-cli` / `novel-api`。这些脚本面向 `scripts/build_release.py` 生成的
Windows onedir 发布包，不依赖 Python 环境，也不需要管理员权限。

脚本列表：

| 脚本 | 用途 |
| --- | --- |
| `packaging/install-cli.ps1` | 安装 `novel-cli`，加入用户 `PATH`，并运行 `--version` 验证 |
| `packaging/install-api.ps1` | 安装 `novel-api`，可选注册登录时自动启动的计划任务 |
| `packaging/verify.ps1` | 校验 `SHA256SUMS.txt` 与 zip 归档完整性 |

> 这些脚本只操作本机文件、当前用户的 `PATH`、用户环境变量和当前用户的计划任务，
> 不需要也不应使用管理员权限运行。

---

### 准备工作

**系统要求**

* Windows 10 / 11（x64）
* Windows PowerShell 5.1（系统自带）或 PowerShell 7+
* 发布包中的可执行文件为 PyInstaller onedir 目录，需要保留同级的 `_internal` 目录

**发布包内容**

`scripts/build_release.py` 会生成如下目录：

```text
novel-downloader-<version>-windows-x64/
├── novel-cli-<version>-windows-x64.zip
├── novel-api-<version>-windows-x64.zip
├── SHA256SUMS.txt
└── manifest.json
```

zip 解压后第一层是 `novel-cli/` 或 `novel-api/` 目录，安装脚本会自动识别。

**执行策略**

如果直接运行脚本被阻止，可使用进程级 `Bypass`（只对本次命令生效）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\install-cli.ps1 -Dir .\dist\novel-cli
```

或者先解除单个文件的锁定：

```powershell
Unblock-File .\packaging\*.ps1
```

---

### 先校验发布包

在安装前先用 `verify.ps1` 校验下载或构建的产物。默认校验当前目录下的
`SHA256SUMS.txt` 以及其中列出的所有文件：

```powershell
cd C:\Path\To\novel-downloader-1.0.0-windows-x64
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Path\To\repo\packaging\verify.ps1
```

在仓库内构建后校验：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\verify.ps1 `
    -ReleaseDir .\dist\release\novel-downloader-1.0.0-windows-x64
```

参数：

| 参数 | 说明 |
| --- | --- |
| `-ReleaseDir <目录>` | 包含 `SHA256SUMS.txt` 的目录，默认当前目录 |
| `-ChecksumFile <文件>` | 直接指定校验文件；其中的文件名相对该校验文件所在目录解析 |
| `-SkipZipTest` | 只比较 SHA-256，不打开 zip 做完整性测试 |

校验内容：

1. 逐行解析标准格式 `<sha256>  <filename>`（支持 `*filename` 二进制标记，忽略空行和 `#` 注释）。
2. 使用 `Get-FileHash -Algorithm SHA256` 比较每个文件。
3. 对校验文件中列出的每个 `.zip`，打开归档并完整读取所有条目，以发现截断或损坏的压缩数据。

任何一项失败都会以非零状态码退出；脚本会继续检查其余条目并汇总失败数量。

---

### 安装 novel-cli

从发布 zip 安装：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\install-cli.ps1 `
    -Zip "$HOME\Downloads\novel-cli-1.0.0-windows-x64.zip"
```

从本地构建目录安装：

```powershell
.\packaging\install-cli.ps1 -Dir .\dist\novel-cli
```

参数：

| 参数 | 说明 |
| --- | --- |
| `-Zip <文件>` | 发布 zip；与 `-Dir` 二选一 |
| `-Dir <目录>` | 已构建的 bundle 目录（包含 `novel-cli.exe`），与 `-Zip` 二选一 |
| `-InstallRoot <目录>` | 安装根目录，默认 `%LOCALAPPDATA%\Programs\novel-downloader` |
| `-NoPath` | 不修改用户 `PATH`，仅用于测试或由其他工具管理 `PATH` 的场景 |
| `-Force` | 替换前强制结束从目标目录启动的 `novel-cli.exe` |

安装行为：

1. 将内容安装到 `%LOCALAPPDATA%\Programs\novel-downloader\novel-cli\`。
2. 把该目录加入**当前用户**的 `PATH`（写入用户环境变量，幂等，不重复添加）。
3. 运行 `%LOCALAPPDATA%\Programs\novel-downloader\novel-cli\novel-cli.exe --version` 验证。
4. 任一步骤失败都以非零状态码退出。

升级时会先把旧目录重命名为临时备份，再替换为新目录。若旧目录因文件被占用无法删除，
脚本会保留备份目录并给出警告。

> `-Force` 会终止正在运行的 `novel-cli`，可能中断正在进行的下载。升级前建议先手动结束任务。

**PATH 生效需要重开终端**

脚本修改的是用户环境变量，已经打开的终端不会自动刷新。安装完成后：

1. 关闭并重新打开 PowerShell / Windows Terminal / cmd。
2. 验证：

```powershell
where.exe novel-cli
novel-cli --version
```

如果仍找不到命令，注销并重新登录，或重启 Explorer。只想在当前会话临时使用时：

```powershell
$env:Path += ";$env:LOCALAPPDATA\Programs\novel-downloader\novel-cli"
```

---

### 安装 novel-api

基本安装方式与 CLI 相同：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\install-api.ps1 `
    -Zip "$HOME\Downloads\novel-api-1.0.0-windows-x64.zip"
```

或：

```powershell
.\packaging\install-api.ps1 -Dir .\dist\novel-api
```

安装位置为 `%LOCALAPPDATA%\Programs\novel-downloader\novel-api\`，默认将
`...\novel-downloader\novel-api` 加入用户 `PATH`，并使用 `novel-api.exe --help`
验证可执行文件。

除与 CLI 相同的 `-Zip`、`-Dir`、`-InstallRoot`、`-NoPath`、`-Force` 外，
还支持：

| 参数 | 说明 |
| --- | --- |
| `-RegisterStartup` | 创建/替换当前用户的登录启动计划任务；必须同时提供 `-Token` |
| `-Token <字符串>` | API 令牌，写入当前用户的 `NOVEL_API_TOKEN` 环境变量；不指定 `-RegisterStartup` 时也会保存，供手动启动使用 |
| `-TaskName <名称>` | 计划任务名称，默认 `novel-downloader-api` |

**注册登录启动**

```powershell
.\packaging\install-api.ps1 -Dir .\dist\novel-api -RegisterStartup -Token (New-Guid).Guid
```

该命令会：

1. 安装/替换 `novel-api`。
2. 将令牌写入 `HKCU\Environment` 中的 `NOVEL_API_TOKEN`（不会回显令牌值）。
3. 使用 `schtasks /Create /SC ONLOGON /F` 创建当前用户的计划任务，任务动作只包含
   `novel-api.exe` 的完整路径。令牌通过用户环境变量在登录时继承，不写入任务命令行。

任务在**下一次登录**时自动启动。需要立即启动时：

```powershell
schtasks /Run /TN "novel-downloader-api"
```

检查服务（默认监听 `127.0.0.1:8081`）：

```powershell
curl.exe -H "X-API-Key: <你的令牌>" http://127.0.0.1:8081/api/v1/health
```

停止任务当前实例（不会删除任务）：

```powershell
schtasks /End /TN "novel-downloader-api"
```

**输出目录注意事项**

计划任务不会设置工作目录。API 的 `general.output_dir` 默认是相对路径
`./downloads`，若配置文件未改为绝对路径，启动时可能相对计划任务的工作目录解析，
导致导出文件落到意外位置或无法创建。

启用 `-RegisterStartup` 前，请在 `%LOCALAPPDATA%\novel_downloader\settings.toml`
中把 `output_dir` 改为绝对路径，例如：

```toml
[general]
output_dir = 'D:\Novels\downloads'
```

**令牌与安全**

* `NOVEL_API_TOKEN` 保存在当前用户环境变量中，未加密，当前用户运行的其他进程可以读取。
* 在命令行传入 `-Token` 可能被 PowerShell 历史记录保存；建议使用随机令牌并妥善保存。
* API 默认只监听 `127.0.0.1`。不要在没有令牌的情况下改为对外监听。
* 计划任务以当前用户身份在登录时运行，注销后停止；不需要管理员权限。

---

### 升级

1. 关闭正在运行的 `novel-cli`，并停止 `novel-api`（或使用 `-Force`）：

   ```powershell
   schtasks /End /TN "novel-downloader-api"
   ```

2. 使用上一节的 `verify.ps1` 校验新发布包。
3. 用相同的安装命令重新安装（脚本会整体替换旧 bundle，数据库/配置不受影响）：

   ```powershell
   .\packaging\install-cli.ps1 -Zip .\novel-cli-<新版本>-windows-x64.zip
   .\packaging\install-api.ps1 -Zip .\novel-api-<新版本>-windows-x64.zip
   ```

4. 计划任务指向固定的安装路径，通常无需重新注册。若令牌有变化或任务被删除，
   重新执行带 `-RegisterStartup -Token <新令牌>` 的安装命令即可。
5. 重新打开终端后运行 `novel-cli --version` 确认版本。

`-NoPath` 仅用于测试；正常升级不需要它，重复运行时 `PATH` 条目不会重复添加。

---

### 卸载与清理

先关闭进程，并删除计划任务/环境变量：

```powershell
schtasks /Delete /TN "novel-downloader-api" /F
[Environment]::SetEnvironmentVariable('NOVEL_API_TOKEN', $null, 'User')
```

从用户 `PATH` 中移除两个安装目录：

```powershell
$root = Join-Path $env:LOCALAPPDATA 'Programs\novel-downloader'
$remove = @((Join-Path $root 'novel-cli'), (Join-Path $root 'novel-api'))
$current = [Environment]::GetEnvironmentVariable('Path', 'User')
$kept = @($current -split ';' | Where-Object {
    $p = $_.Trim().Trim('"')
    $p -and ($remove -notcontains $p.TrimEnd('\'))
})
[Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'User')
```

删除程序文件：

```powershell
Remove-Item -LiteralPath (Join-Path $env:LOCALAPPDATA 'Programs\novel-downloader') -Recurse -Force
```

如需删除用户数据（可选，删除后配置、下载记录、缓存和登录状态都会丢失）：

| 内容 | 默认位置 |
| --- | --- |
| 配置、状态、缓存、日志 | `%LOCALAPPDATA%\novel_downloader\` |
| 默认下载/导出目录 | 由 `general.output_dir` 指定，默认相对路径 `./downloads` |
| 音乐数据与登录会话 | `%LOCALAPPDATA%\novel_downloader\music\` |
| 自定义数据根目录 | `NOVEL_HOME` / `NOVEL_MUSIC_HOME` 环境变量指向的位置 |

清理清单：

1. 结束 `novel-cli` / `novel-api` 进程。
2. 删除计划任务 `novel-downloader-api`。
3. 删除用户环境变量 `NOVEL_API_TOKEN`。
4. 从用户 `PATH` 删除 `...\novel-downloader\novel-cli` 和 `...\novel-downloader\novel-api`。
5. 删除 `%LOCALAPPDATA%\Programs\novel-downloader`。
6. 按需删除 `%LOCALAPPDATA%\novel_downloader` 和自定义输出目录。
7. 若使用了自定义 `-InstallRoot`，按相同步骤删除对应目录及其 `PATH` 条目。

除用户环境变量（`PATH`、`NOVEL_API_TOKEN`）外，脚本不会写入其他注册表项，也不会安装系统服务。

---

### 常见问题

**提示 `... is running ...`**

目标目录中的可执行文件仍在运行或被其他进程占用。关闭对应程序后重试，或加 `-Force`
强制结束。`-Force` 会中断正在进行的下载/服务请求。

**`novel-cli` 不是可识别的命令**

`PATH` 修改只对新启动的进程生效。重开终端；仍无效时注销/重新登录。也可先使用完整路径：

```powershell
& "$env:LOCALAPPDATA\Programs\novel-downloader\novel-cli\novel-cli.exe" --version
```

**`-Zip` 解压后找不到 exe**

安装脚本会在 zip 根目录或其唯一一级子目录中查找 `novel-cli.exe` /
`novel-api.exe`。如果同一个 zip 中存在多个候选目录，请解压后改用 `-Dir` 指向正确的
bundle 目录。

**杀毒软件误报**

PyInstaller 生成的独立可执行文件偶尔会被误报。请从可信来源获取发布包并先校验
`SHA256SUMS.txt`；必要时添加杀毒软件排除项，或改用 `pip install` 方式安装。

**计划任务已创建但没有启动**

任务在用户登录时触发，不会立即运行。用 `schtasks /Run /TN "novel-downloader-api"` 立即启动，
或注销后重新登录。若令牌刚写入，重新登录可确保进程获得最新的用户环境变量。

**端口 8081 被占用**

默认监听 `127.0.0.1:8081`。可以先手动运行 `novel-api --port <端口>` 排查，或在计划任务
属性中修改任务的参数/操作。脚本本身使用默认参数注册任务。