# Copia GOOGLE_CREDENTIALS_JSON (una sola linea) al portapapeles para Railway
param(
    [string]$JsonPath = ""
)

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $JsonPath) {
    $JsonPath = Join-Path $Root "credentials\google_service_account.json"
}
if (-not (Test-Path $JsonPath)) {
    $Downloads = Join-Path $env:USERPROFILE "Downloads\proyecto-agente-tatami-336cf776f776.json"
    if (Test-Path $Downloads) {
        $JsonPath = $Downloads
    }
}

if (-not (Test-Path $JsonPath)) {
    Write-Error "No se encuentra JSON. Pasa -JsonPath o coloca credentials\google_service_account.json"
    exit 1
}

$py = @"
import json, sys
p = sys.argv[1]
with open(p, encoding='utf-8') as f:
    obj = json.load(f)
print(json.dumps(obj, separators=(',', ':')))
"@

$oneLine = python -c $py $JsonPath
if ($LASTEXITCODE -ne 0) {
    Write-Error "JSON invalido en $JsonPath"
    exit 1
}

$oneLine | Set-Clipboard
Write-Host "OK: GOOGLE_CREDENTIALS_JSON minificado copiado al portapapeles."
Write-Host "Archivo: $JsonPath"
Write-Host "Longitud: $($oneLine.Length) caracteres"
Write-Host ""
Write-Host "En Railway -> Variables:"
Write-Host "  1. GOOGLE_CREDENTIALS_JSON -> pegar (Ctrl+V) -> Save"
Write-Host "  2. ELIMINAR GOOGLE_CREDENTIALS_PATH si existe"
Write-Host "  3. SPREADSHEET_ID = 1rTVMfsOBssx2R-Sbuj1SRx9NZSd_hinEa9IK_ahGqZY"
