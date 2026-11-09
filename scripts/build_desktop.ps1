param(
    [string]$PythonPath = '',
    [switch]$SkipTests
)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ($env:OS -ne 'Windows_NT') { throw 'The desktop executable must be built on Windows.' }
if (-not $PythonPath) {
    $PythonPath = (& py -3.12 -c 'import sys; print(sys.executable)').Trim()
    if ($LASTEXITCODE -ne 0 -or -not $PythonPath) { throw 'Python 3.12 is required for this build.' }
}
Push-Location $taskRoot
try {
    & $PythonPath -c "import struct, sys, importlib.metadata; assert struct.calcsize('P') == 8, 'Use 64-bit Python'; print('Python', sys.version.split()[0]); print('PyInstaller', importlib.metadata.version('pyinstaller'))"
    if ($LASTEXITCODE -ne 0) { throw 'Use 64-bit Python with the official PyInstaller package installed.' }
    foreach ($taskRequired in @('clients/python/synofind_dsm/desktop.py', 'clients/python/synofind_dsm/desktop_web/index.html', 'clients/python/synofind_dsm/desktop_web/app.js', 'clients/python/synofind_dsm/desktop_web/styles.css')) {
        if (-not (Test-Path -LiteralPath (Join-Path $taskRoot $taskRequired) -PathType Leaf)) { throw "Desktop source is incomplete: $taskRequired" }
    }
    if (-not $SkipTests) {
        Push-Location (Join-Path $taskRoot 'clients/python')
        try {
            & $PythonPath -m unittest discover -s tests -v
            if ($LASTEXITCODE -ne 0) { throw 'Client and desktop tests failed.' }
        } finally { Pop-Location }
    }
    $taskBuildRoot = Join-Path $taskRoot 'work/desktop-build'
    New-Item -ItemType Directory -Force -Path $taskBuildRoot | Out-Null
    # This generated wrapper contains no application logic. Importing the package
    # preserves relative imports while PyInstaller discovers the real entry point.
    $taskLauncher = Join-Path $taskBuildRoot 'launcher.py'
    $taskLauncherText = "from multiprocessing import freeze_support`nfrom synofind_dsm.desktop import main`n`nif __name__ == '__main__':`n    freeze_support()`n    raise SystemExit(main())`n"
    [System.IO.File]::WriteAllText($taskLauncher, $taskLauncherText, [System.Text.UTF8Encoding]::new($false))
    $taskArguments = @(
        '-m', 'PyInstaller', '--noconfirm', '--onefile', '--console', '--noupx',
        '--name', 'SynoFind', '--distpath', $taskBuildRoot,
        '--workpath', (Join-Path $taskBuildRoot 'analysis'),
        '--specpath', $taskBuildRoot,
        '--paths', (Join-Path $taskRoot 'clients/python'),
        '--add-data', ((Join-Path $taskRoot 'clients/python/synofind_dsm/desktop_web') + ':synofind_dsm/desktop_web'),
        $taskLauncher
    )
    & $PythonPath @taskArguments
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    & $PythonPath scripts/package_desktop.py --exe (Join-Path $taskBuildRoot 'SynoFind.exe') --verify-exe
    if ($LASTEXITCODE -ne 0) { throw 'Desktop packaging failed.' }
} finally { Pop-Location }
