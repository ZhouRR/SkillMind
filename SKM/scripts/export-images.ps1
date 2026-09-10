<#
.SYNOPSIS
構築済み application image を images/images.tar に保存する。
.DESCRIPTION
build/pull、container 起動、.env 読取は行わない。-Force は既存 archive の置換だけを許可する。
完全 offline の初回配備には -IncludeInfrastructure で local 基盤 image も同梱する。
#>
[CmdletBinding()]
param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "..\images"),
    [switch]$Force,
    [switch]$IncludeInfrastructure
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (-not (Get-Command docker -CommandType Application -ErrorAction SilentlyContinue)) {
    throw "Docker CLI is missing. Start Rancher Desktop with the Moby engine."
}
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$archivePath = Join-Path ([System.IO.Path]::GetFullPath($OutputDirectory)) "images.tar"
if (Test-Path -LiteralPath $archivePath) {
    $existing = Get-Item -LiteralPath $archivePath -Force
    if ($existing.PSIsContainer -or ($existing.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw "images.tar must be a regular file, not a directory or link."
    }
    if (-not $Force) { throw "images.tar already exists. Use -Force to replace it." }
}
function Invoke-Docker {
    param([string[]]$DockerArgs)
    # stderr は利用者の terminal へ残し、JSON の stdout と混ぜない。
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $result = @(& docker @DockerArgs)
        $code = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousPreference }
    if ($code -ne 0) { throw "Docker '$($DockerArgs[0]) $($DockerArgs[1])' failed (exit code $code)." }
    return $result
}
$images = @("skillmind/backend:0.1.0", "skillmind/web:0.1.0")
if ($IncludeInfrastructure) {
    # 基盤 tag は Compose 正本から取得し、設定の補間・dotenv 読取は無効化する。
    $environment = @{
        COMPOSE_DISABLE_ENV_FILE = "true"
        COMPOSE_ENV_FILES = $null
        COMPOSE_FILE = $null
        COMPOSE_PROFILES = "skillmind-export-no-profiles"
    }
    $original = @{}
    try {
        foreach ($key in $environment.Keys) {
            $original[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
            [Environment]::SetEnvironmentVariable($key, $environment[$key], "Process")
        }
        $config = ((Invoke-Docker @("compose", "--project-directory", $projectRoot,
            "--file", (Join-Path $projectRoot "compose.yml"), "config",
            "--no-interpolate", "--no-env-resolution", "--format", "json")) -join "`n") | ConvertFrom-Json
        foreach ($service in @("postgres", "redis", "object-storage", "object-storage-init")) {
            $images += [string]$config.services.$service.image
        }
    }
    finally {
        foreach ($key in $original.Keys) { [Environment]::SetEnvironmentVariable($key, $original[$key], "Process") }
    }
}
foreach ($reference in $images) {
    Invoke-Docker @("image", "inspect", $reference, "--format", "{{.Id}}") | Out-Null
}
New-Item -ItemType Directory -Path (Split-Path -Parent $archivePath) -Force | Out-Null
$temporaryPath = $archivePath + "." + [Guid]::NewGuid().ToString("N") + ".partial"
Write-Host "Saving local images..."
Invoke-Docker (@("image", "save", "--output", $temporaryPath) + $images) | Out-Null
if ((Get-Item -LiteralPath $temporaryPath).Length -le 0) { throw "Docker produced an empty archive." }
# 保存完了までは旧 archive を維持する。失敗した partial は調査用に残す。
if ($Force -and (Test-Path -LiteralPath $archivePath)) {
    [System.IO.File]::Replace($temporaryPath, $archivePath, [System.Management.Automation.Language.NullString]::Value)
} else {
    [System.IO.File]::Move($temporaryPath, $archivePath)
}
Write-Host "Exported: $archivePath"
Write-Host "Copy images.tar, compose.yml and Makefile to the server; keep its .env."
