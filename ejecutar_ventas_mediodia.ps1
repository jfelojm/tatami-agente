# Ejecuta ventas_smartmenu para el dia calendario ANTERIOR en America/Guayaquil (~12:00).
# Misma regla de fecha que pipeline_diario.py (sin --fecha). Pensado para Task Scheduler.
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

$fecha = & $Py -c "from datetime import datetime, timedelta; from zoneinfo import ZoneInfo; z=ZoneInfo('America/Guayaquil'); d=datetime.now(z).date(); print((d-timedelta(days=1)).isoformat())"

Write-Host "========================================"
Write-Host "Ventas Smart Menu — fecha $fecha (ayer Ecuador, alineado con pipeline_diario)"
Write-Host "========================================"

& $Py "$Root\ventas_smartmenu.py" --fecha $fecha
exit $LASTEXITCODE