# Ejecuta pipeline_diario.py (ventas -> reconciliar grid vs hist -> descargo -> ...).
# Pensado para Task Scheduler. Opciones extra se pasan al .py, ej:
#   .\ejecutar_pipeline_diario.ps1
#   .\ejecutar_pipeline_diario.ps1 --skip-ventas
#   .\ejecutar_pipeline_diario.ps1 --strict-ventas
#   .\ejecutar_pipeline_diario.ps1 --skip-reconciliar

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$env:PYTHONIOENCODING = "utf-8"
$Py = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Error "No se encuentra venv en $Py"
    exit 1
}

Write-Host "========================================"
Write-Host "Pipeline diario Tatami — $(Get-Date -Format 'yyyy-MM-dd HH:mm') (local)"
Write-Host "========================================"

& $Py "$Root\pipeline_diario.py" @args
exit $LASTEXITCODE
