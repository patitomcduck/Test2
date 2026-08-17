$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "=== Collector POS Desktop Builder ===" -ForegroundColor Cyan
Write-Host "Building Windows x64 desktop package..."

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
  throw "Python 3.12 x64 no está instalado o no está en PATH."
}
$version = & $pythonCmd.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($version -ne '3.12') {
  throw "El build de referencia usa Python 3.12 x64. Detectado: $version"
}

if (Test-Path .venv-build) { Remove-Item .venv-build -Recurse -Force }
& $pythonCmd.Source -m venv .venv-build
& .\.venv-build\Scripts\python.exe -m pip install --upgrade pip wheel
& .\.venv-build\Scripts\python.exe -m pip install -r requirements-desktop.txt

if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path dist) { Remove-Item dist -Recurse -Force }
if (Test-Path dist-installer) { Remove-Item dist-installer -Recurse -Force }

& .\.venv-build\Scripts\pyinstaller.exe --noconfirm --clean CollectorPOS.spec

$isccCandidates = @(
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
  Write-Host ""
  Write-Host "PyInstaller terminó. Falta Inno Setup 6 para generar Setup.exe." -ForegroundColor Yellow
  Write-Host "La aplicación portable está en dist\CollectorPOS\CollectorPOS.exe"
  exit 0
}

New-Item -ItemType Directory -Force dist-installer | Out-Null
& $iscc installer\CollectorPOS.iss
Write-Host ""
Write-Host "LISTO: revisa dist-installer\CollectorPOS-Setup-3.0.0-preview.exe" -ForegroundColor Green
