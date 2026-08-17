$ErrorActionPreference = "Stop"
$data = Join-Path $env:LOCALAPPDATA "CollectorPOS"
New-Item -ItemType Directory -Force $data | Out-Null
$envFile = Join-Path $data "collector.env"

$current = @{}
if (Test-Path $envFile) {
  Get-Content $envFile | ForEach-Object {
    if ($_ -match '^([^#][^=]*)=(.*)$') { $current[$matches[1].Trim()] = $matches[2] }
  }
}
if (-not $current.ContainsKey('SECRET_KEY')) {
  $bytes = New-Object byte[] 32
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $current['SECRET_KEY'] = ($bytes | ForEach-Object ToString x2) -join ''
}
$key = Read-Host "API key de JustTCG (Enter para conservar la actual)"
if ($key) { $current['JUSTTCG_API_KEY'] = $key }
elseif (-not $current.ContainsKey('JUSTTCG_API_KEY')) { $current['JUSTTCG_API_KEY'] = '' }

@('SMTP_HOST','SMTP_PORT','SMTP_USERNAME','SMTP_PASSWORD','SMTP_FROM','SMTP_USE_TLS') | ForEach-Object {
  if (-not $current.ContainsKey($_)) { $current[$_] = if ($_ -eq 'SMTP_PORT') {'587'} elseif ($_ -eq 'SMTP_USE_TLS') {'1'} else {''} }
}

$current.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" } | Set-Content -Encoding UTF8 $envFile
Write-Host "Configuración guardada en $envFile" -ForegroundColor Green
