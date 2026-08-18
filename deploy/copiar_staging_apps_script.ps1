# Copia el Apps Script de Masters Sheets (traslados + facturas + admin) al portapapeles
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ScriptPath = Join-Path $Root "deploy\STAGING_APPS_SCRIPT_COMPLETO.gs"

if (-not (Test-Path $ScriptPath)) {
    Write-Error "No se encuentra $ScriptPath"
    exit 1
}

$raw = Get-Content -Raw -Encoding UTF8 $ScriptPath
$raw | Set-Clipboard
Write-Host "OK: STAGING_APPS_SCRIPT_COMPLETO.gs copiado al portapapeles."
Write-Host ""
Write-Host "Masters Sheets (staging) -> Extensiones -> Apps Script:"
Write-Host "  1. Borrar archivos .gs viejos"
Write-Host "  2. Un solo archivo Code.gs -> pegar todo (Ctrl+V)"
Write-Host "  3. Guardar -> recargar la hoja (F5)"
Write-Host ""
Write-Host "Libro staging: 1TJu70BNG4i3it4y51Eg3YlDNswLkh1QGRt6v-qAyexU"
Write-Host "NO pegar en DataMaestra (ese usa tatami_maestro_unificado.gs solo conteo)."
