# Ejecuta ventas_smartmenu para la fecha LOCAL de hoy (~12:00).
# Pensado para Task Scheduler: ver ventas del día tras el cierre nocturno (viernes–sábado ~1am).
# Uso manual: .\ejecutar_ventas_mediodia.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$env:PYTHONIOENCODING = "utf-8"
$Py = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Error "No se encuentra venv en $Py"
    exit 1
}

# Fecha local Windows (YYYY-MM-DD)
$fecha = Get-Date -Format "yyyy-MM-dd"

Write-Host "========================================"
Write-Host "Ventas Smart Menu — fecha $fecha (local)"
Write-Host "========================================"

& $Py "$Root\ventas_smartmenu.py" --fecha $fecha
exit $LASTEXITCODE
