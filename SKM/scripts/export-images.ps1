<#
.SYNOPSIS
Skillmind の Docker Compose 関連 image を一つの tar file へ書き出す。

.DESCRIPTION
compose.yaml から image 一覧を取得して重複を除去し、local Docker に存在する image を
docker image save で書き出す。Skillmind application image の不足は失敗とし、既存配備で
再利用できる PostgreSQL、Redis、MinIO などの不足は警告して書き出し対象から除外する。

.PARAMETER EnvFile
共用 Compose runner が補間と Backend 注入へ使う同一環境 file。
省略時は shell の ENV_FILE、次に repository root の .env。相対 path は root 基準。

.PARAMETER ProjectName
Compose project 名。省略時は shell の COMPOSE_PROJECT_NAME、次に skillmind。

.PARAMETER PythonCommand
Python 3.12 以上の実行 file 名または単一 path。引数を含む command 文字列は受け付けない。

.PARAMETER OutputDirectory
tar file の出力先。既定値は repository root の images directory。

.PARAMETER ArchiveName
出力する tar file 名。既定値は skillmind-images.tar。

.PARAMETER Force
書き出し成功後に同名 tar file を原子的に置換する。失敗時は既存 file を保持する。
#>
[CmdletBinding()]
param(
    [string]$EnvFile,
    [string]$ProjectName,
    [string]$PythonCommand = "python",
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "..\images"),
    [string]$ArchiveName = "skillmind-images.tar",
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$composeRunner = Join-Path $PSScriptRoot "compose.py"
$resolvedOutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)

if (-not (Get-Command docker -CommandType Application -ErrorAction SilentlyContinue)) {
    throw "docker command が見つかりません。Docker Desktop を起動してから再実行してください。"
}
$pythonExecutable = Get-Command $PythonCommand -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $pythonExecutable) {
    throw "Python 3.12 以上の実行 file を -PythonCommand で指定してください。"
}
if (-not (Test-Path -LiteralPath $composeRunner -PathType Leaf)) {
    throw "共用 Compose runner が見つかりません。"
}
if ([System.IO.Path]::GetFileName($ArchiveName) -ne $ArchiveName -or
    [System.IO.Path]::GetExtension($ArchiveName) -ne ".tar") {
    throw "ArchiveName には path を含まない .tar file 名を指定してください。"
}

# dotenv を別実装で解釈せず、通常操作/配備と同じ runner に対象の確定を任せる。
$composeArguments = @("-B", $composeRunner)
if ($PSBoundParameters.ContainsKey("EnvFile")) {
    $composeArguments += @("--env-file", $EnvFile)
}
if ($PSBoundParameters.ContainsKey("ProjectName")) {
    $composeArguments += @("--project-name", $ProjectName)
}
$composeArguments += @("--", "config", "--images")
$images = @(& $pythonExecutable.Source @composeArguments)
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose 設定から image 一覧を取得できませんでした。"
}
$images = @($images | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object -Unique)
if ($images.Count -eq 0) {
    throw "書き出し対象の Docker image がありません。"
}

# 必須 application の役割を固定し、第三者 image の名前 prefix から推測しない。
# 通常 wrapper は shell/.env の image selector をこの既定 tag へ固定する。
$applicationImages = @{
    backend = "skillmind/backend:0.1.0"
    web = "skillmind/web:0.1.0"
}
foreach ($requiredImage in $applicationImages.Values) {
    if ($images -cnotcontains $requiredImage) {
        throw "Compose 設定に必須 application image がありません。"
    }
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
    elseif ($applicationImages.Values -ccontains $image) {
        # 配備 archive に application image が欠けると旧 version のまま起動するため fail closed とする。
        $missingApplicationImages.Add($image)
    }
    else {
        $skippedImages.Add($image)
    }
}
if ($missingApplicationImages.Count -gt 0) {
    throw "Local Docker に Skillmind application image がありません。先に共用 Compose runner で build を実行してください:`n$($missingApplicationImages -join "`n")"
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
}

Write-Host "Export images:"
$images | ForEach-Object { Write-Host "  $_" }

# 同じ directory の新規 file に完成させてから置換し、途中失敗で旧成品を失わない。
$temporaryPath = Join-Path $resolvedOutputDirectory (".skillmind-export-" + [Guid]::NewGuid().ToString("N") + ".tar")
$ownsTemporaryFile = $false
try {
    $reservation = [System.IO.File]::Open($temporaryPath, [System.IO.FileMode]::CreateNew)
    $ownsTemporaryFile = $true
    $reservation.Dispose()
    & docker image save --output $temporaryPath @images
    if ($LASTEXITCODE -ne 0 -or (Get-Item -LiteralPath $temporaryPath).Length -le 0) {
        throw "Docker image の書き出しに失敗しました。既存 archive は変更していません。"
    }

    if (Test-Path -LiteralPath $archivePath) {
        if (-not $Force) {
            throw "出力 file が既に存在します。既存 archive は変更していません。"
        }
        # 置換が非対応の filesystem では失敗とし、削除後 rename には退行しない。
        [System.IO.File]::Replace($temporaryPath, $archivePath, $null)
    }
    else {
        [System.IO.File]::Move($temporaryPath, $archivePath)
    }
}
finally {
    if ($ownsTemporaryFile -and (Test-Path -LiteralPath $temporaryPath)) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
}

$archive = Get-Item -LiteralPath $archivePath
Write-Host "Exported: $($archive.FullName) ($([Math]::Round($archive.Length / 1MB, 2)) MiB)"
