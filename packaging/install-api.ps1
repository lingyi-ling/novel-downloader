<#
.SYNOPSIS
    Install the standalone novel-api bundle for the current Windows user.

.DESCRIPTION
    Installs a PyInstaller onedir bundle (a release .zip or a dist directory)
    into %LOCALAPPDATA%\Programs\novel-downloader\novel-api, adds that
    directory to the user PATH, and verifies the executable with --help.

    The optional -RegisterStartup switch stores the API token in the
    per-user NOVEL_API_TOKEN environment variable and creates a per-user
    Task Scheduler job that starts novel-api at logon. The task command line
    contains only the executable path; the token is inherited from the user
    environment, so it is not written into the task definition.

    No administrator rights are required.

.PARAMETER Zip
    Path to novel-api-<version>-windows-x64.zip (the archive produced by
    scripts/build_release.py). Mutually exclusive with -Dir.

.PARAMETER Dir
    Path to an already-built bundle directory such as dist\novel-api.
    Mutually exclusive with -Zip.

.PARAMETER InstallRoot
    Installation root. Defaults to %LOCALAPPDATA%\Programs\novel-downloader.
    The bundle is placed in the novel-api subdirectory.

.PARAMETER NoPath
    Do not modify the user PATH. Intended for tests and CI.

.PARAMETER Force
    Stop any novel-api.exe running from the install destination before
    replacing it. Without this switch the script fails instead of killing a
    running server.

.PARAMETER RegisterStartup
    Create or replace the logon task and store the token in the user
    environment. Requires -Token.

.PARAMETER Token
    API token stored as the per-user NOVEL_API_TOKEN environment variable.
    Required with -RegisterStartup; when supplied without it the token is
    still persisted for manual starts.

.PARAMETER TaskName
    Name of the Task Scheduler job. Defaults to novel-downloader-api.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\install-api.ps1 -Zip .\novel-api-1.0.0-windows-x64.zip

.EXAMPLE
    .\packaging\install-api.ps1 -Dir .\dist\novel-api -RegisterStartup -Token 'change-me'

.NOTES
    The logon task does not set a working directory. If settings.toml keeps a
    relative general.output_dir (the default is ./downloads), configure an
    absolute path before enabling -RegisterStartup, otherwise exports may be
    written relative to the Task Scheduler working directory.
#>
[CmdletBinding()]
param(
    [string]$Zip,
    [string]$Dir,
    [string]$InstallRoot,
    [switch]$NoPath,
    [switch]$Force,
    [switch]$RegisterStartup,
    [string]$Token,
    [string]$TaskName = 'novel-downloader-api'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$AppName = 'novel-api'
$ExeName = 'novel-api.exe'
$TokenVariable = 'NOVEL_API_TOKEN'

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

function Set-UserToken {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ($Value -match '[\r\n]') {
        throw 'The token must not contain newline characters.'
    }
    [Environment]::SetEnvironmentVariable($TokenVariable, $Value, 'User')
    Write-Host "Stored $TokenVariable for the current user (value not shown)."
}

function Register-LogonTask {
    param(
        [Parameter(Mandatory = $true)][string]$ExecutablePath,
        [Parameter(Mandatory = $true)][string]$TaskName
    )

    if ($TaskName -match '["\r\n]') {
        throw 'TaskName must not contain double quotes or newline characters.'
    }

    $schtasks = Join-Path $env:SystemRoot 'System32\schtasks.exe'
    if (-not (Test-Path -LiteralPath $schtasks -PathType Leaf)) {
        throw "schtasks.exe not found at '$schtasks'."
    }

    # Build the raw command line ourselves: PowerShell's native-argument
    # re-quoting would otherwise mangle an embedded quoted executable path.
    $taskCommand = $ExecutablePath
    if ($taskCommand -match '\s') {
        $taskCommand = '"' + $taskCommand + '"'
    }
    $arguments = '/Create /TN "' + $TaskName + '" /SC ONLOGON /F /TR ' + $taskCommand

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $schtasks
    $startInfo.Arguments = $arguments
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = [System.Diagnostics.Process]::Start($startInfo)
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()

    if ($process.ExitCode -ne 0) {
        $detail = ($stderr + $stdout).Trim()
        throw "schtasks /Create failed with exit code $($process.ExitCode): $detail"
    }
    return (($stdout + $stderr).Trim())
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

    $hasToken = -not [string]::IsNullOrWhiteSpace($Token)
    if ($RegisterStartup -and -not $hasToken) {
        throw '-RegisterStartup requires -Token so the API is not exposed without authentication.'
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
    $null = & $destinationExe --help 2>&1
    $helpExit = $LASTEXITCODE
    if ($helpExit -ne 0) {
        throw "novel-api --help failed with exit code $helpExit."
    }

    if ($NoPath) {
        Write-Host 'Skipped user PATH update because -NoPath was specified.'
    } else {
        Add-UserPathEntry -Entry $destination
    }

    if ($hasToken) {
        Set-UserToken -Value $Token
    }

    $taskResult = $null
    if ($RegisterStartup) {
        Write-Host "Registering logon task '$TaskName'..."
        $taskResult = Register-LogonTask -ExecutablePath $destinationExe -TaskName $TaskName
    }

    Write-Host ''
    Write-Host 'novel-api installed successfully.' -ForegroundColor Green
    Write-Host "  Executable: $destinationExe"
    if ($hasToken) {
        Write-Host "  Token     : stored in user environment variable $TokenVariable"
    }
    if ($RegisterStartup) {
        Write-Host "  Startup   : task '$TaskName' runs at logon for the current user"
    }
    if (-not $NoPath) {
        Write-Host ''
        Write-Host 'Open a NEW terminal for the PATH change to take effect, then run: novel-api --help'
    }
    if ($taskResult) {
        Write-Host ''
        Write-Host $taskResult
    }
    if (-not $RegisterStartup) {
        Write-Host ''
        Write-Host 'Start the server manually with: novel-api'
        Write-Host "To remove a previously registered task: schtasks /Delete /TN `"$TaskName`" /F"
    }
    exit 0
} catch {
    [Console]::Error.WriteLine('ERROR: ' + $_.Exception.Message)
    exit 1
}