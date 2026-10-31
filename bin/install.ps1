<#
.SYNOPSIS
    One-line installer for Novel Downloader on Windows.

.DESCRIPTION
    Intended to be run straight from the web:

        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/lingyi-ling/novel-downloader/main/bin/install.ps1 | iex"

    Strategy:
      1. If a GitHub release exists, download the novel-cli and
         NovelDownloader bundles and install them per-user
         (%LOCALAPPDATA%\Programs\novel-downloader), adding novel-cli to the
         user PATH and creating Start-menu + desktop shortcuts.
      2. When no release is published (or downloads fail), fall back to a
         pip user install when Python 3.11+ is available; otherwise print
         clear next steps.

    No administrator rights are required.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Repo = 'lingyi-ling/novel-downloader'
$InstallRoot = Join-Path $env:LOCALAPPDATA 'Programs\novel-downloader'

function Write-Step([string]$msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg) { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2([string]$msg) { Write-Host "    $msg" -ForegroundColor Yellow }

function Get-LatestRelease {
    try {
        $resp = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" `
            -Headers @{ Accept = 'application/vnd.github+json' } -TimeoutSec 15
        return $resp
    } catch {
        return $null
    }
}

function Add-UserPath([string]$dir) {
    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (($current -split ';') -contains $dir) { return }
    [Environment]::SetEnvironmentVariable(
        'Path', (($current.TrimEnd(';') + ';' + $dir).TrimStart(';')), 'User')
    $env:Path = "$env:Path;$dir"
}

function New-Shortcut([string]$target, [string]$name) {
    try {
        $ws = New-Object -ComObject WScript.Shell
        foreach ($folder in @(
            [Environment]::GetFolderPath('Desktop'),
            [Environment]::GetFolderPath('Programs'))) {
            $lnk = $ws.CreateShortcut((Join-Path $folder "$name.lnk"))
            $lnk.TargetPath = $target
            $lnk.WorkingDirectory = Split-Path -Parent $target
            $lnk.IconLocation = "$target,0"
            $lnk.Save()
        }
    } catch {
        Write-Warn2 "shortcut creation skipped: $($_.Exception.Message)"
    }
}

function Install-FromRelease($release) {
    $tag = $release.tag_name
    Write-Step "Downloading release $tag"
    $tmp = Join-Path $env:TEMP ("noveldl-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null

    $assets = @($release.assets | Where-Object { $_.name -match 'novel-cli.*windows.*\.zip$' })
    $deskAssets = @($release.assets | Where-Object { $_.name -match 'NovelDownloader.*\.zip$' })
    if (-not $assets) { throw 'novel-cli windows zip not found in the release' }

    $cliZip = Join-Path $tmp $assets[0].name
    Invoke-WebRequest -Uri $assets[0].browser_download_url -OutFile $cliZip -TimeoutSec 300
    Write-Ok "downloaded $($assets[0].name)"

    Write-Step 'Installing novel-cli'
    $cliRoot = Join-Path $InstallRoot 'novel-cli'
    if (Test-Path $cliRoot) { Remove-Item -Recurse -Force $cliRoot }
    Expand-Archive -Path $cliZip -DestinationPath $cliRoot -Force
    # the zip contains the onedir bundle; normalise nested directory
    $exe = Get-ChildItem -Path $cliRoot -Recurse -Filter 'novel-cli.exe' | Select-Object -First 1
    if (-not $exe) { throw 'novel-cli.exe missing after extraction' }
    $cliBin = Split-Path -Parent $exe.FullName
    Add-UserPath $cliBin
    & (Join-Path $cliBin 'novel-cli.exe') --version | ForEach-Object { Write-Ok "verified: $_" }

    if ($deskAssets) {
        Write-Step 'Installing desktop client'
        $deskZip = Join-Path $tmp $deskAssets[0].name
        Invoke-WebRequest -Uri $deskAssets[0].browser_download_url -OutFile $deskZip -TimeoutSec 600
        $deskRoot = Join-Path $InstallRoot 'NovelDownloader'
        if (Test-Path $deskRoot) { Remove-Item -Recurse -Force $deskRoot }
        Expand-Archive -Path $deskZip -DestinationPath $deskRoot -Force
        $deskExe = Get-ChildItem -Path $deskRoot -Recurse -Filter 'NovelDownloader.exe' |
            Select-Object -First 1
        if ($deskExe) {
            New-Shortcut $deskExe.FullName 'Novel Downloader'
            Write-Ok 'desktop shortcuts created (desktop + Start menu)'
        }
    }

    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue

    Write-Host ''
    Write-Host 'Novel Downloader installed.' -ForegroundColor Green
    Write-Host '  CLI        : novel-cli (new terminals; restart your shell if needed)'
    if ($deskAssets) { Write-Host '  Desktop    : "Novel Downloader" on your desktop / Start menu' }
    Write-Host '  Quick start: https://github.com/lingyi-ling/novel-downloader#-quick-start'
}

function Install-FromPip {
    Write-Step 'No published release found; trying pip instead'
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        Write-Warn2 'Python 3.11+ is required and was not found.'
        Write-Warn2 'Install Python from https://www.python.org/downloads/ and re-run,'
        Write-Warn2 'or grab a packaged build from the Releases page once published.'
        return
    }
    $parts = (& python -c "import sys; print(sys.version_info[0], sys.version_info[1])") -split '\s+'
    if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) {
        Write-Warn2 "Python $($parts[0]).$($parts[1]) found; 3.11+ is required."
        return
    }
    & python -m pip install -U --user 'novel-downloader[api,music]'
    if ($LASTEXITCODE -eq 0) {
        Write-Ok 'pip install finished'
        Write-Host '  CLI        : novel-cli (ensure the Python Scripts dir is on PATH)'
        Write-Host '  Desktop    : novel-desktop'
    }
}

Write-Host ''
Write-Host 'Novel Downloader installer' -ForegroundColor Magenta
Write-Host '--------------------------'
$release = Get-LatestRelease
if ($release -and $release.assets) {
    Install-FromRelease $release
} else {
    Install-FromPip
}
