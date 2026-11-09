param(
    [string]$Version = '0.1.0',
    [int]$Build = 1,
    [string]$GoPath = '',
    [switch]$SkipTests
)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $GoPath) {
    $taskPortableGo = Join-Path $taskRoot '.tools\go\bin\go.exe'
    if (Test-Path -LiteralPath $taskPortableGo) { $GoPath = $taskPortableGo }
    else { $GoPath = (Get-Command go -ErrorAction Stop).Source }
}
$taskOldEnv = @{}
foreach ($taskName in @('CGO_ENABLED', 'GOOS', 'GOARCH', 'GOAMD64')) {
    $taskOldEnv[$taskName] = [Environment]::GetEnvironmentVariable($taskName, 'Process')
}
Push-Location $taskRoot
try {
    Push-Location (Join-Path $taskRoot 'web')
    try {
        & npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency install failed.' }
        if (-not $SkipTests) {
            & npm.cmd test
            if ($LASTEXITCODE -ne 0) { throw 'Frontend tests failed.' }
        }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    } finally { Pop-Location }
    & py -3.12 scripts/collect_notices.py --go-path $GoPath
    if ($LASTEXITCODE -ne 0) { throw 'Third-party notice collection failed.' }
    if (-not $SkipTests) {
        & $GoPath test ./...
        if ($LASTEXITCODE -ne 0) { throw 'Backend tests failed.' }
        & py -3.12 scripts/test_packaging.py
        if ($LASTEXITCODE -ne 0) { throw 'Packaging tests failed.' }
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $taskRoot 'work\build') | Out-Null
    $env:CGO_ENABLED = '0'
    $env:GOOS = 'linux'
    $env:GOARCH = 'amd64'
    $env:GOAMD64 = 'v1'
    & $GoPath build -trimpath -ldflags "-s -w -X main.version=$Version" -o work/build/synofind ./cmd/synofind
    if ($LASTEXITCODE -ne 0) { throw 'Linux binary build failed.' }
    & py -3.12 scripts/build_spk.py --binary work/build/synofind --version $Version --build $Build
    if ($LASTEXITCODE -ne 0) { throw 'SPK packaging failed.' }
} finally {
    foreach ($taskName in $taskOldEnv.Keys) {
        [Environment]::SetEnvironmentVariable($taskName, $taskOldEnv[$taskName], 'Process')
    }
    Pop-Location
}
