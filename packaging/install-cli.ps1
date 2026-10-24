<#
.SYNOPSIS
    Install the standalone novel-cli bundle for the current Windows user.

.DESCRIPTION
    Installs a PyInstaller onedir bundle (a release .zip or a dist directory)
    into %LOCALAPPDATA%\Programs\novel-downloader\novel-cli, adds that
    directory to the user PATH, and verifies the executable with --version.

    The operation is idempotent: an existing installation is replaced as a
    whole and the PATH entry is only added once. No administrator rights are
    required.

.PARAMETER Zip
    Path to novel-cli-<version>-windows-x64.zip (the archive produced by
    scripts/build_release.py). Mutually exclusive with -Dir.

.PARAMETER Dir
    Path to an already-built bundle directory such as dist\novel-cli.
    Mutually exclusive with -Zip.

.PARAMETER InstallRoot
    Installation root. Defaults to %LOCALAPPDATA%\Programs\novel-downloader.
    The bundle is placed in the novel-cli subdirectory.

.PARAMETER NoPath
    Do not modify the user PATH. Intended for tests and CI.

.PARAMETER Force
    Stop any novel-cli.exe running from the install destination before
    replacing it. Without this switch the script fails with a clear message
    instead of terminating a download in progress.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\install-cli.ps1 -Zip .\novel-cli-1.0.0-windows-x64.zip

.EXAMPLE
    .\packaging\install-cli.ps1 -Dir .\dist\novel-cli
#>
[CmdletBinding()]
param(
    [string]$Zip,
    [string]$Dir,
    [string]$InstallRoot,
    [switch]$NoPath,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$AppName = 'novel-cli'
$ExeName = 'novel-cli.exe'

function Normalize-PathText {
    param([Parameter(Mandatory = $true)][string]$Path)
    $expanded = [Environment]::ExpandEnvironmentVariables($Path)
    return [System.IO.Path]::GetFullPath($expanded).TrimEnd('\')
}

function Test-UserPathEntry {
    param(
        [string]$PathList,
        [Parameter(Mandatory = $true)][string]$Entry
    )
    if ([string]::IsNullOrWhiteSpace($PathList)) {
        return $false
    }
    $target = Normalize-PathText -Path $Entry
    foreach ($raw in $PathList.Split(';')) {
        $item = $raw.Trim().Trim('"')
        if ([string]::IsNullOrWhiteSpace($item)) {
            continue
        }
        try {
            $normalized = Normalize-PathText -Path $item
        } catch {
            continue
        }
        if ([string]::Equals($normalized, $target, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Add-UserPathEntry {
    param([Parameter(Mandatory = $true)][string]$Entry)

    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (Test-UserPathEntry -PathList $current -Entry $Entry) {
        Write-Host "User PATH already contains: $Entry"
        return
    }

    if ([string]::IsNullOrWhiteSpace($current)) {
        $updated = $Entry
    } else {
        $updated = $current.TrimEnd(';') + ';' + $Entry
    }

    # The Win32 environment block limit is 32767 characters; refuse to write
    # a truncated PATH instead of silently corrupting it.
    if ($updated.Length -gt 32000) {
        throw "The user PATH would exceed 32000 characters. Add '$Entry' to PATH manually."
    }

    [Environment]::SetEnvironmentVariable('Path', $updated, 'User')
    Write-Host "Added to user PATH: $Entry"
}

function Resolve-BundleRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ExeName
    )

    if (Test-Path -LiteralPath (Join-Path $Root $ExeName) -PathType Leaf) {
        return $Root
    }

    $candidates = @()
    foreach ($child in @(Get-ChildItem -LiteralPath $Root -Directory -Force)) {
        if (Test-Path -LiteralPath (Join-Path $child.FullName $ExeName) -PathType Leaf) {
            $candidates += $child.FullName
        }
    }

    if ($candidates.Count -eq 1) {
        return $candidates[0]
    }
    if ($candidates.Count -eq 0) {
        throw "Could not find $ExeName in '$Root' or in one of its direct subdirectories."
    }
    throw "Found $ExeName in multiple subdirectories of '$Root'; point -Dir at the bundle directory itself."
}

function New-BundleStaging {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$AppName,
        [Parameter(Mandatory = $true)][string]$ExeName
    )

    $staging = Join-Path $Parent ('.staging-' + $AppName + '-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    try {
        foreach ($item in @(Get-ChildItem -LiteralPath $Source -Force)) {
            Copy-Item -LiteralPath $item.FullName -Destination $staging -Recurse -Force
        }
        if (-not (Test-Path -LiteralPath (Join-Path $staging $ExeName) -PathType Leaf)) {
            throw "The bundle source '$Source' did not produce $ExeName in the staging directory."
        }
    } catch {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }
    return $staging
}

function Get-RunningBundleProcess {
    param([Parameter(Mandatory = $true)][string]$ExecutablePath)

    $processName = [System.IO.Path]::GetFileNameWithoutExtension($ExecutablePath)
    $target = Normalize-PathText -Path $ExecutablePath
    $found = @()

    foreach ($process in @(Get-Process -Name $processName -ErrorAction SilentlyContinue)) {
        $processPath = $null
        try {
            $processPath = $process.Path
        } catch {
            $processPath = $null
        }
        if ([string]::IsNullOrWhiteSpace($processPath)) {
            continue
        }
        try {
            $normalized = Normalize-PathText -Path $processPath
        } catch {
            continue
        }
        if ([string]::Equals($normalized, $target, [System.StringComparison]::OrdinalIgnoreCase)) {
            $found += $process
        }
    }
    return $found
}

function Stop-RunningBundleIfNeeded {
    param(
        [Parameter(Mandatory = $true)][string]$ExecutablePath,
        [Parameter(Mandatory = $true)][string]$AppName,
        [switch]$Force
    )

    $running = @(Get-RunningBundleProcess -ExecutablePath $ExecutablePath)
    if ($running.Count -eq 0) {
        return
    }

    $ids = ($running | ForEach-Object { $_.Id }) -join ', '
    if (-not $Force) {
        throw "$AppName is running from '$ExecutablePath' (PID: $ids). Close it and retry, or rerun with -Force to terminate it."
    }

    Write-Host "Stopping running $AppName process(es): $ids"
    foreach ($process in $running) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }

    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        if (@(Get-RunningBundleProcess -ExecutablePath $ExecutablePath).Count -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 200
    }
    throw "Could not stop $AppName (PID: $ids) within 10 seconds."
}

function Install-Bundle {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$AppName,
        [Parameter(Mandatory = $true)][string]$ExeName
    )

    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Path $parent -Force | Out-Null

    $staging = New-BundleStaging -Source $Source -Parent $parent -AppName $AppName -ExeName $ExeName
    $backup = Join-Path $parent ('.backup-' + $AppName + '-' + [Guid]::NewGuid().ToString('N'))
    $movedOld = $false

    try {
        if (Test-Path -LiteralPath $Destination) {
            Move-Item -LiteralPath $Destination -Destination $backup
            $movedOld = $true
        }
        Move-Item -LiteralPath $staging -Destination $Destination
    } catch {
        if ($movedOld -and -not (Test-Path -LiteralPath $Destination) -and (Test-Path -LiteralPath $backup)) {
            Move-Item -LiteralPath $backup -Destination $Destination -ErrorAction SilentlyContinue
        }
        throw
    } finally {
        if (Test-Path -LiteralPath $staging) {
            Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    if ($movedOld -and (Test-Path -LiteralPath $backup)) {
        try {
            Remove-Item -LiteralPath $backup -Recurse -Force
        } catch {
            Write-Warning "Previous installation kept at '$backup' because it could not be removed: $($_.Exception.Message)"
            Write-Warning 'Close any running instance and delete that directory manually.'
        }
    }
}

try {
    if ([Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        throw 'This installer only supports Windows.'
    }
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw '%LOCALAPPDATA% is not set; cannot determine the default install location.'
    }

    $hasZip = -not [string]::IsNullOrWhiteSpace($Zip)
    $hasDir = -not [string]::IsNullOrWhiteSpace($Dir)
    if ($hasZip -eq $hasDir) {
        throw 'Specify exactly one of -Zip <archive> or -Dir <bundle directory>.'
    }

    if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
        $InstallRoot = Join-Path $env:LOCALAPPDATA 'Programs\novel-downloader'
    }
    $installRootFull = [System.IO.Path]::GetFullPath($InstallRoot)
    $destination = Join-Path $installRootFull $AppName
    $destinationExe = Join-Path $destination $ExeName

    if (Test-Path -LiteralPath $destinationExe -PathType Leaf) {
        Stop-RunningBundleIfNeeded -ExecutablePath $destinationExe -AppName $AppName -Force:$Force
    }

    $extractRoot = $null
    try {
        if ($hasZip) {
            $zipFull = (Resolve-Path -LiteralPath $Zip -ErrorAction Stop).Path
            if (-not (Test-Path -LiteralPath $zipFull -PathType Leaf)) {
                throw "Zip archive not found: $Zip"
            }
            $extractRoot = Join-Path $installRootFull ('.extract-' + [Guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null
            Write-Host "Extracting $zipFull"
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            [System.IO.Compression.ZipFile]::ExtractToDirectory($zipFull, $extractRoot)
            $source = Resolve-BundleRoot -Root $extractRoot -ExeName $ExeName
        } else {
            $dirFull = (Resolve-Path -LiteralPath $Dir -ErrorAction Stop).Path
            if (-not (Test-Path -LiteralPath $dirFull -PathType Container)) {
                throw "Bundle directory not found: $Dir"
            }
            $source = Resolve-BundleRoot -Root $dirFull -ExeName $ExeName
        }

        Write-Host "Installing $AppName to $destination"
        Install-Bundle -Source $source -Destination $destination -AppName $AppName -ExeName $ExeName
    } finally {
        if ($extractRoot -and (Test-Path -LiteralPath $extractRoot)) {
            Remove-Item -LiteralPath $extractRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    Write-Host 'Verifying installed executable...'
    $versionOutput = & $destinationExe --version 2>&1
    $versionExit = $LASTEXITCODE
    $versionText = (($versionOutput | Out-String).Trim())
    if ($versionExit -ne 0) {
        throw "novel-cli --version failed with exit code $versionExit. Output: $versionText"
    }
    if ([string]::IsNullOrWhiteSpace($versionText)) {
        throw 'novel-cli --version produced no output.'
    }

    if ($NoPath) {
        Write-Host 'Skipped user PATH update because -NoPath was specified.'
    } else {
        Add-UserPathEntry -Entry $destination
    }

    Write-Host ''
    Write-Host 'novel-cli installed successfully.' -ForegroundColor Green
    Write-Host "  Executable: $destinationExe"
    Write-Host "  Version   : $versionText"
    if (-not $NoPath) {
        Write-Host ''
        Write-Host 'Open a NEW terminal for the PATH change to take effect, then run: novel-cli --version'
    }
    exit 0
} catch {
    [Console]::Error.WriteLine('ERROR: ' + $_.Exception.Message)
    exit 1
}