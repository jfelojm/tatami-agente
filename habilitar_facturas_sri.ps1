# Habilita tareas SRI (10:00 AM / 18:00 PM). Ejecutar PowerShell COMO ADMINISTRADOR.
#
#   .\habilitar_facturas_sri.ps1
#
# Re-registra con sesión interactiva (Chrome visible) y las activa.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    Write-Host "ERROR: ejecute PowerShell como Administrador." -ForegroundColor Red
    Write-Host "  Click derecho en PowerShell -> Ejecutar como administrador"
    Write-Host "  cd `"$Root`""
    Write-Host "  .\habilitar_facturas_sri.ps1"
    exit 1
}

Write-Host "Registrando tareas SRI (modo interactivo)..."
& (Join-Path $Root "registrar_facturas_sri_tareas.ps1") -SinAdmin

foreach ($name in @("TatamiFacturasSRI_AM", "TatamiFacturasSRI_PM")) {
    Enable-ScheduledTask -TaskName $name | Out-Null
    Write-Host "OK  $name habilitada"
}

Write-Host ""
Write-Host "Estado:"
Get-ScheduledTask -TaskName TatamiFacturasSRI_AM, TatamiFacturasSRI_PM | ForEach-Object {
    $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
    Write-Host "  $($_.TaskName)  Estado=$($_.State)  Logon=$($_.Principal.LogonType)  Proxima=$($info.NextRunTime)"
}

Write-Host ""
Write-Host "Requisitos:"
Write-Host "  - PC encendida y sesion Windows iniciada a las 10:00 y 18:00"
Write-Host "  - .env: SRI_VENTANA_DIAS=3, SRI_CONSULTA_MODO=auto, SRI_PORTAL_HEADLESS=0"
Write-Host "  - Renovar sesion SRI si falla: .\ejecutar_facturas_sri.ps1 --init-portal-session"
