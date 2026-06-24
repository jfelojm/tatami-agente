# Configura SRI con Chrome + consulta manual (login y clic CONSULTAR por usted).
#
# Paso 1 - guardar sesion en perfil Chrome (primera vez o si expira):
#   .\configurar_sri_chrome.ps1
#
# Paso 2 - descargar facturas (ventana .env SRI_VENTANA_DIAS):
#   .\configurar_sri_chrome.ps1 -Descargar
#   .\configurar_sri_chrome.ps1 -Descargar -Corrida PM

param(
    [switch]$Descargar,
    [string]$Corrida = "MANUAL"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$ScriptSri = Join-Path $Root "ejecutar_facturas_sri.ps1"

Write-Host "========================================"
Write-Host "SRI Tatami - Chrome + consulta manual"
Write-Host "========================================"
Write-Host ""
Write-Host "Variables en .env:"
Write-Host "  SRI_PORTAL_BROWSER=chrome"
Write-Host "  SRI_CONSULTA_MODO=manual"
Write-Host "  SRI_PORTAL_HEADLESS=0"
Write-Host ""

if ($Descargar) {
    Write-Host "Iniciando descarga (usted hace clic en CONSULTAR por cada dia)..."
    Write-Host ""
    & $ScriptSri $Corrida "--portal-visible" "--consulta-manual"
    exit $LASTEXITCODE
}

Write-Host "Paso 1: guardar sesion Chrome (login manual en portal SRI)"
Write-Host ""
& $ScriptSri "--init-portal-session"
exit $LASTEXITCODE
