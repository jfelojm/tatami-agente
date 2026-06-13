# Copia GOOGLE_CREDENTIALS_JSON al portapapeles para pegar en Railway
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$JsonPath = Join-Path $Root "credentials\google_service_account.json"

if (-not (Test-Path $JsonPath)) {
    Write-Error "No se encuentra $JsonPath"
    exit 1
}

$raw = Get-Content -Raw -Encoding UTF8 $JsonPath
$raw | Set-Clipboard
Write-Host "OK: JSON de Google copiado al portapapeles."
Write-Host "En Railway -> Variables -> GOOGLE_CREDENTIALS_JSON -> pegar (Ctrl+V) -> Save"
