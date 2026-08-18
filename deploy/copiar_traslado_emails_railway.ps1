# Copia TRASLADO_SHEETS_EMAILS al portapapeles para Railway
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$EnvPath = Join-Path $Root ".env"

if (-not (Test-Path $EnvPath)) {
    Write-Error "No se encuentra $EnvPath"
    exit 1
}

$m = Select-String -Path $EnvPath -Pattern "^TRASLADO_SHEETS_EMAILS=(.+)$" | Select-Object -First 1
if (-not $m) {
    Write-Error "No se encontro TRASLADO_SHEETS_EMAILS en .env"
    exit 1
}

$val = $m.Matches[0].Groups[1].Value.Trim()
$val | Set-Clipboard
Write-Host "OK: TRASLADO_SHEETS_EMAILS copiado al portapapeles."
Write-Host ""
Write-Host "En Railway -> tatami-agente -> Variables:"
Write-Host "  TRASLADO_SHEETS_EMAILS = pegar (Ctrl+V) -> Save"
Write-Host ""
Write-Host "Valor:"
Write-Host $val
