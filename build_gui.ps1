$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw 'Create .venv first with py -3 -m venv .venv, then install requirements-build.txt.'
}
& $projectPython -m PyInstaller --noconfirm --distpath $PSScriptRoot --workpath (Join-Path $PSScriptRoot 'build\pyinstaller') (Join-Path $PSScriptRoot 'MO2Fish.spec')
if ($LASTEXITCODE -ne 0) { throw 'MO2Fish build failed; inspect the output above.' }
Write-Host "Built $(Join-Path $PSScriptRoot 'MO2Fish.exe')"
