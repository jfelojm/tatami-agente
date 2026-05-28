# Ejecuta pipeline_diario.py (ventas -> reconciliar grid vs hist -> descargo -> ...).
# Pensado para Task Scheduler. Opciones extra se pasan al .py, ej:
#   .\ejecutar_pipeline_diario.ps1
#   .\ejecutar_pipeline_diario.ps1 --skip-ventas
#   .\ejecutar_pipeline_diario.ps1 --strict-ventas
#   .\ejecutar_pipeline_diario.ps1 --skip-reconciliar
#
# Task Scheduler no muestra salida en una terminal abierta: todo va en segundo plano.
# Este script escribe además en logs\pipeline_diario_YYYYMMDD.log (mismo día = append).
# Para "ver en vivo" antes de la hora programada, en otra ventana PowerShell:
#   Set-Location "...\tatami-agente\logs"
#   Get-Content .\pipeline_diario_20260511.log -Wait -Tail 40
# (cambia la fecha al día de la ejecución)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$env:PYTHONIOENCODING = "utf-8"
$Py = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Error "No se encuentra venv en $Py"
    exit 1
}

$LogDir = Join-Path $Root "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}
$LogFile = Join-Path $LogDir ("pipeline_diario_{0:yyyyMMdd}.log" -f (Get-Date))
try {
    Start-Transcript -Path $LogFile -Append -Encoding utf8 | Out-Null
} catch {
    # Si ya hay transcript activo, seguir solo por consola
}

Write-Host "========================================"
Write-Host "Pipeline diario Tatami -- $(Get-Date -Format 'yyyy-MM-dd HH:mm') (local)"
Write-Host "Log: $LogFile"
Write-Host "========================================"

$code = 1
try {
    & $Py "$Root\pipeline_diario.py" @args
    $code = $LASTEXITCODE
} finally {
    try { Stop-Transcript | Out-Null } catch { }
}
exit $code

