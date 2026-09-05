<#
.SYNOPSIS
ProjectMind の Docker Compose 関連 image を一つの tar file へ書き出す。

.DESCRIPTION
compose.yaml から image 一覧を取得して重複を除去し、local Docker に存在する image を
docker image save で書き出す。ProjectMind application image の不足は失敗とし、既存配備で
再利用できる PostgreSQL、Redis、MinIO などの不足は警告して書き出し対象から除外する。

.PARAMETER EnvFile
Compose の変数展開に使用する環境 file。既定値は repository root の .env。

.PARAMETER OutputDirectory
tar file の出力先。既定値は repository root の images directory。

.PARAMETER ArchiveName
出力する tar file 名。既定値は projectmind-images.tar。

.PARAMETER Force
同名 tar file が存在する場合に上書きする。
#>
[CmdletBinding()]
param(
    [string]$EnvFile = (Join-Path $PSScriptRoot "..\.env"),
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "..\images"),
    [string]$ArchiveName = "projectmind-images.tar",
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$composeFile = Join-Path $projectRoot "compose.yaml"
$resolvedEnvFile = [System.IO.Path]::GetFullPath($EnvFile)
$resolvedOutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "docker command が見つかりません。Docker Desktop を起動してから再実行してください。"
}
if (-not (Test-Path -LiteralPath $composeFile -PathType Leaf)) {
    throw "Compose file が見つかりません: $composeFile"
}
if (-not (Test-Path -LiteralPath $resolvedEnvFile -PathType Leaf)) {
    throw "環境 file が見つかりません: $resolvedEnvFile"
}
if ([System.IO.Path]::GetFileName($ArchiveName) -ne $ArchiveName -or
    [System.IO.Path]::GetExtension($ArchiveName) -ne ".tar") {
    throw "ArchiveName には path を含まない .tar file 名を指定してください。"
}

# Compose を image 一覧の正本とし、service 追加時に script の手動更新を不要にする。
$images = @(
    & docker compose --env-file $resolvedEnvFile -f $composeFile config --images
)
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose 設定から image 一覧を取得できませんでした。"
}
$images = @($images | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object -Unique)
if ($images.Count -eq 0) {
    throw "書き出し対象の Docker image がありません。"
}

$availableImages = [System.Collections.Generic.List[string]]::new()
$missingApplicationImages = [System.Collections.Generic.List[string]]::new()
$skippedImages = [System.Collections.Generic.List[string]]::new()
foreach ($image in $images) {
    # Windows PowerShell 5.1 は native stderr を ErrorRecord 化するため、存在確認中だけ
    # Stop を解除し、Docker の exit code を唯一の判定根拠として全不足 image を集約する。
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & docker image inspect $image *> $null
        $inspectExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($inspectExitCode -eq 0) {
        $availableImages.Add($image)
    }
    elseif ($image.StartsWith("projectmind/", [System.StringComparison]::OrdinalIgnoreCase)) {
        # 配備 archive に application image が欠けると旧 version のまま起動するため fail closed とする。
        $missingApplicationImages.Add($image)
    }
    else {
        $skippedImages.Add($image)
    }
}
if ($missingApplicationImages.Count -gt 0) {
    throw "Local Docker に ProjectMind application image がありません。先に docker compose build を実行してください:`n$($missingApplicationImages -join "`n")"
}
if ($skippedImages.Count -gt 0) {
    Write-Warning "Local Docker に存在しない第三者 image を書き出し対象から除外します:`n$($skippedImages -join "`n")"
}
$images = @($availableImages)
if ($images.Count -eq 0) {
    throw "書き出し可能な Docker image がありません。"
}

New-Item -ItemType Directory -Path $resolvedOutputDirectory -Force | Out-Null
$archivePath = Join-Path $resolvedOutputDirectory $ArchiveName
if (Test-Path -LiteralPath $archivePath) {
    if (-not $Force) {
        throw "出力 file は既に存在します。上書きする場合は -Force を指定してください: $archivePath"
    }
    Remove-Item -LiteralPath $archivePath -Force
}

Write-Host "Export images:"
$images | ForEach-Object { Write-Host "  $_" }

# 一つの archive にまとめ、server 側では一回の docker load で配備できるようにする。
& docker image save --output $archivePath @images
if ($LASTEXITCODE -ne 0) {
    # 失敗した不完全 archive を残さず、server へ誤配備されることを防ぐ。
    Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue
    throw "Docker image の書き出しに失敗しました。"
}

$archive = Get-Item -LiteralPath $archivePath
Write-Host "Exported: $($archive.FullName) ($([Math]::Round($archive.Length / 1MB, 2)) MiB)"
