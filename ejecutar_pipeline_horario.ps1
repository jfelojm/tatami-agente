# ejecutar_pipeline_horario.ps1
# Pipeline horario Tatami Bao Bar
# Logica de fecha:
#   hora local >= 12 -> fecha = hoy (ventas del dia en curso)
#   hora local <  12 -> fecha = ayer (cierra el dia anterior)
# Se llama cada hora via Task Scheduler (TatamiPipelineHorario).
# Log: logs/pipeline_horario_YYYY-MM-DD_HH.log (Tee-Object, no depende de Start-Transcript)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"

$Py = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Error "No se encuentra venv en $Py"
    exit 1
}

$Hora = (Get-Date).Hour
if ($Hora -ge 12) {
    $Fecha = (Get-Date).ToString("yyyy-MM-dd")
    $Label = "HOY"
} else {
    $Fecha = (Get-Date).AddDays(-1).ToString("yyyy-MM-dd")
    $Label = "AYER"
}

$LogDir = Join-Path $Root "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}
$HoraStr = (Get-Date).ToString("HH")
$LogFile = Join-Path $LogDir "pipeline_horario_${Fecha}_${HoraStr}.log"

function Write-LogLine([string]$Msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

Write-LogLine "========================================"
Write-LogLine "Pipeline horario Tatami - inicio $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
Write-LogLine "Fecha objetivo ($Label): $Fecha"
Write-LogLine "Log: $LogFile"
Write-LogLine "========================================"

$code = 1
try {
    & $Py "$Root\pipeline_diario.py" --fecha $Fecha --skip-reconciliar 2>&1 | Tee-Object -FilePath $LogFile -Append
    $code = $LASTEXITCODE
} catch {
    Write-LogLine "ERROR PowerShell: $_"
    $code = 1
}

Write-LogLine "========================================"
Write-LogLine "Pipeline horario - fin codigo_salida=$code"
Write-LogLine "========================================"
exit $code
