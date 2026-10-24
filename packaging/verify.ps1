<#
.SYNOPSIS
    Verify release archives against SHA256SUMS.txt and test zip integrity.

.DESCRIPTION
    Reads a SHA256SUMS.txt file in the standard "<sha256>  <filename>" format,
    hashes every listed file with Get-FileHash, and (by default) opens every
    listed .zip archive and fully reads each entry to detect truncated or
    corrupted data.

    Every problem is reported; the script exits non-zero if anything failed.

.PARAMETER ReleaseDir
    Directory containing SHA256SUMS.txt and the release archives. Defaults to
    the current directory.

.PARAMETER ChecksumFile
    Explicit path to a checksum file. Relative archive names are resolved
    against this file's directory, not against the current directory.

.PARAMETER SkipZipTest
    Only compare SHA-256 hashes; do not open and read the zip archives.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\verify.ps1 -ReleaseDir .\dist\release\novel-downloader-1.0.0-windows-x64

.EXAMPLE
    .\packaging\verify.ps1 -ChecksumFile .\release\SHA256SUMS.txt
#>
[CmdletBinding()]
param(
    [string]$ReleaseDir = '.',
    [string]$ChecksumFile,
    [switch]$SkipZipTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Failure {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "[FAIL] $Message" -ForegroundColor Red
}

function Test-ZipArchive {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$DisplayName
    )

    if (-not ('System.IO.Compression.ZipFile' -as [type])) {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
    }

    $archive = $null
    try {
        $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
        $entryCount = 0
        [long]$totalBytes = 0
        $buffer = New-Object byte[] 81920

        foreach ($entry in $archive.Entries) {
            $normalized = $entry.FullName.Replace('\', '/')
            if ($normalized.EndsWith('/')) {
                continue
            }
            $stream = $entry.Open()
            try {
                while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $totalBytes += $read
                }
            } finally {
                $stream.Dispose()
            }
            $entryCount++
        }

        $sizeMiB = [math]::Round($totalBytes / 1MB, 1)
        Write-Host "[OK] $DisplayName - $entryCount entries, $sizeMiB MiB readable"
        return $true
    } catch {
        Write-Failure "$DisplayName - zip integrity check failed: $($_.Exception.Message)"
        return $false
    } finally {
        if ($null -ne $archive) {
            $archive.Dispose()
        }
    }
}

try {
    if ([string]::IsNullOrWhiteSpace($ChecksumFile)) {
        $releasePath = (Resolve-Path -LiteralPath $ReleaseDir -ErrorAction Stop).Path
        if (-not (Test-Path -LiteralPath $releasePath -PathType Container)) {
            throw "Release directory not found: $ReleaseDir"
        }
        $ChecksumFile = Join-Path $releasePath 'SHA256SUMS.txt'
    } else {
        $ChecksumFile = (Resolve-Path -LiteralPath $ChecksumFile -ErrorAction Stop).Path
    }

    if (-not (Test-Path -LiteralPath $ChecksumFile -PathType Leaf)) {
        throw "Checksum file not found: $ChecksumFile"
    }

    $checksumDir = Split-Path -Parent $ChecksumFile
    Write-Host "Checksum file: $ChecksumFile"

    $entries = @()
    $parseFailures = 0
    $lineNumber = 0
    foreach ($line in [System.IO.File]::ReadAllLines($ChecksumFile)) {
        $lineNumber++
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith('#')) {
            continue
        }
        if ($trimmed -match '^([0-9A-Fa-f]{64})\s+\*?(.+)$') {
            $entries += [pscustomobject]@{
                Expected = $matches[1].ToLowerInvariant()
                File     = $matches[2].Trim()
            }
        } else {
            Write-Failure "line $lineNumber is not a valid checksum entry: $trimmed"
            $parseFailures++
        }
    }

    if ($entries.Count -eq 0) {
        throw "No checksum entries found in $ChecksumFile"
    }

    $hashFailures = 0
    $zipFailures = 0
    $zips = @()

    foreach ($entry in $entries) {
        $filePath = Join-Path $checksumDir $entry.File
        if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
            Write-Failure "$($entry.File) - file is missing"
            $hashFailures++
            continue
        }

        try {
            $actual = (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash.ToLowerInvariant()
        } catch {
            Write-Failure "$($entry.File) - could not compute SHA-256: $($_.Exception.Message)"
            $hashFailures++
            continue
        }

        if ($actual -eq $entry.Expected) {
            Write-Host "[OK] $($entry.File) - SHA-256 matches"
            if ($entry.File -match '\.zip$') {
                $zips += [pscustomobject]@{
                    Path        = $filePath
                    DisplayName = $entry.File
                }
            }
        } else {
            Write-Failure "$($entry.File) - SHA-256 mismatch (expected $($entry.Expected), got $actual)"
            $hashFailures++
        }
    }

    if (-not $SkipZipTest) {
        if ($zips.Count -eq 0) {
            Write-Host 'No zip archives listed in the checksum file; skipping zip integrity test.'
        }
        foreach ($zip in $zips) {
            if (-not (Test-ZipArchive -Path $zip.Path -DisplayName $zip.DisplayName)) {
                $zipFailures++
            }
        }
    } else {
        Write-Host 'Skipping zip integrity test because -SkipZipTest was specified.'
    }

    $totalFailures = $parseFailures + $hashFailures + $zipFailures
    Write-Host ''
    if ($totalFailures -gt 0) {
        [Console]::Error.WriteLine("Verification FAILED: $parseFailures malformed line(s), $hashFailures hash failure(s), $zipFailures zip failure(s).")
        exit 1
    }

    Write-Host "Verification passed: $($entries.Count) checksum(s) verified." -ForegroundColor Green
    exit 0
} catch {
    [Console]::Error.WriteLine('ERROR: ' + $_.Exception.Message)
    exit 1
}