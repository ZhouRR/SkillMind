<#
.SYNOPSIS
Windows/Rancher Desktop で build し、Linux 向け offline release directory を作る。
.DESCRIPTION
宿主 Python/Node/make は不要。既存 release と runtime .env は上書きしない。
-WebOnly は Backend を再 build せず、現用 Backend と同じ image の存在を要求する。
-IncludeInfrastructure は初回用の第三者 image も pull/save する。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9._-]*$')]
    [string]$Version,
    [ValidateSet("linux/amd64", "linux/arm64")]
    [string]$Platform = "linux/amd64",
    [string]$ContextPath = "/skillmind",
    [string]$EnvFile = ".env.example",
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "..\images"),
    [switch]$IncludeInfrastructure,
    [switch]$WebOnly,
    [switch]$SkipBuild
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (-not [System.IO.Path]::IsPathRooted($EnvFile)) { $EnvFile = Join-Path $projectRoot $EnvFile }
$resolvedEnvFile = (Resolve-Path -LiteralPath $EnvFile).Path
if (-not (Test-Path -LiteralPath $resolvedEnvFile -PathType Leaf)) { throw "Environment file is missing." }
if ($ContextPath -notmatch '^/[a-zA-Z0-9/_-]*[a-zA-Z0-9_-]$') { throw "Use a non-root context path without a trailing slash." }
if (-not (Get-Command docker -CommandType Application -ErrorAction SilentlyContinue)) {
    throw "Docker CLI is missing. Start Rancher Desktop with the Moby engine."
}

function Invoke-Docker {
    param([string[]]$DockerArgs)
    # Native stderr は設定値を含み得るため、公開するのは固定 error だけにする。
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $result = @(& docker @DockerArgs 2>$null)
        $code = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousPreference }
    if ($code -ne 0) { throw "Docker command failed; inspect the build environment privately." }
    return $result
}
function Write-Lf {
    param([string]$Path, [string]$Content)
    # Windows PowerShell 5.1 でも Linux script は BOM なし UTF-8/LF とする。
    [System.IO.File]::WriteAllText($Path, ($Content -replace "`r`n", "`n"), [System.Text.UTF8Encoding]::new($false))
}
$applicationImages = @{
    backend = "skillmind/backend:0.1.0"
    web = "skillmind/web:0.1.0"
}
$environment = @{
    SKM_COMPOSE_ENV_FILE = $resolvedEnvFile
    COMPOSE_PROJECT_NAME = "skillmind"
    COMPOSE_DISABLE_ENV_FILE = "true"
    COMPOSE_COMPATIBILITY = "false"
    # Windows の空文字は環境変数削除になるため、未使用の明示 profile で dotenv を遮断する。
    COMPOSE_PROFILES = "skillmind-export-no-profiles"
    COMPOSE_FILE = $null
    COMPOSE_ENV_FILES = $null
    COMPOSE_PATH_SEPARATOR = $null
    SKM_BACKEND_IMAGE = $applicationImages.backend
    SKM_WEB_IMAGE = $applicationImages.web
    SKILLMIND_CONTEXT_PATH = $ContextPath
    DOCKER_DEFAULT_PLATFORM = $Platform
}
$originalEnvironment = @{}
$releaseRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
$releasePath = Join-Path $releaseRoot ("skillmind-" + $Version)
if (Test-Path -LiteralPath $releasePath) { throw "Release already exists; choose another version." }
$temporaryPath = Join-Path $releaseRoot (".skillmind-export-" + [Guid]::NewGuid().ToString("N"))
$composeArguments = @("compose", "--project-directory", $projectRoot,
    "--file", (Join-Path $projectRoot "compose.yaml"), "--env-file", $resolvedEnvFile,
    "--project-name", "skillmind")
try {
    foreach ($key in $environment.Keys) {
        $originalEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
        [Environment]::SetEnvironmentVariable($key, $environment[$key], "Process")
    }
    Invoke-Docker @("version", "--format", "{{.Server.Os}}") | ForEach-Object {
        if ($_ -ne "linux") { throw "The Rancher daemon must run Linux containers." }
    }
    $configText = Invoke-Docker ($composeArguments + @("config", "--format", "json"))
    $config = ($configText -join "`n") | ConvertFrom-Json
    if (-not $SkipBuild) {
        Write-Host "Building $Platform images..."
        $buildServices = @("api", "web")
        if ($WebOnly) { $buildServices = @("web") }
        Invoke-Docker ($composeArguments + @("build") + $buildServices) | Out-Null
    }
    $images = @($applicationImages.backend, $applicationImages.web)
    if ($IncludeInfrastructure) {
        foreach ($service in @("postgres", "redis", "object-storage", "object-storage-init")) {
            $reference = $config.services.$service.image
            Invoke-Docker @("pull", "--platform", $Platform, $reference) | Out-Null
            $images += $reference
        }
    }
    $identities = @{}
    foreach ($reference in $images) {
        $details = ((Invoke-Docker @("image", "inspect", $reference)) -join "`n") | ConvertFrom-Json
        $item = @($details)[0]
        if ($item.Id -notmatch '^sha256:[0-9a-f]{64}$' -or "$($item.Os)/$($item.Architecture)" -ne $Platform) {
            throw "Image identity/platform mismatch. Rebuild for the target server architecture."
        }
        $identities[$reference] = $item.Id
        if ($reference -eq $applicationImages.web -and $item.Config.Labels.'org.skillmind.context-path' -ne $ContextPath) {
            throw "Web context path does not match. Rebuild the Web image."
        }
    }
    New-Item -ItemType Directory -Path (Join-Path $temporaryPath "scripts") -Force | Out-Null
    Write-Host "Saving release images..."
    $archivePath = Join-Path $temporaryPath "images.tar"
    Invoke-Docker (@("image", "save", "--output", $archivePath) + $images) | Out-Null
    if ((Get-Item -LiteralPath $archivePath).Length -le 0) { throw "Image archive is empty." }
    $archiveHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash.ToLowerInvariant()
    Invoke-Docker @("run", "--rm", "--pull", "never", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--mount", "type=bind,src=$archivePath,dst=/release/images.tar,readonly",
        "--entrypoint", "python", $identities[$applicationImages.backend],
        "/opt/skillmind-deploy/deploy_checks.py", "archive", "--checksum", $archiveHash,
        "--backend", $identities[$applicationImages.backend], "--web", $identities[$applicationImages.web]) | Out-Null
    $files = @("compose.yaml", "Makefile", ".env.example", "scripts/compose.sh", "scripts/deploy.sh")
    foreach ($relative in $files) {
        Write-Lf (Join-Path $temporaryPath $relative) ([System.IO.File]::ReadAllText((Join-Path $projectRoot $relative)))
    }
    $manifest = @(
        "FORMAT=1",
        "BACKEND_IMAGE_ID=$($identities[$applicationImages.backend])",
        "WEB_IMAGE_ID=$($identities[$applicationImages.web])",
        "PLATFORM=$Platform",
        "CONTEXT_PATH=$ContextPath",
        "VERSION=$Version"
    ) -join "`n"
    Write-Lf (Join-Path $temporaryPath "release.env") ($manifest + "`n")
    $checksums = foreach ($relative in ($files + @("release.env", "images.tar"))) {
        $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $temporaryPath $relative)).Hash.ToLowerInvariant()
        "$digest  $relative"
    }
    Write-Lf (Join-Path $temporaryPath "SHA256SUMS") (($checksums -join "`n") + "`n")
    # Directory rename は完成後だけ。失敗した staging は診断用に残し、旧 release は消さない。
    [System.IO.Directory]::Move($temporaryPath, $releasePath)
    $releaseHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $releasePath "SHA256SUMS")).Hash.ToLowerInvariant()
    Write-Host "Release: $releasePath"
    Write-Host "RELEASE_SHA256=$releaseHash"
    Write-Host "Transfer the whole directory. Keep this checksum separately; never copy runtime .env."
}
finally {
    foreach ($key in $originalEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $originalEnvironment[$key], "Process")
    }
}
