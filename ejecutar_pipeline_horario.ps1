# Pipeline horario secuencial (7:00-00:00 EC, cada hora).
# Task Scheduler: TatamiPipelineHorario (registrar_estrategia_tareas.ps1)

$ErrorActionPreference = "Continue"
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
$LogFile = Join-Path $LogDir ("pipeline_horario_{0:yyyyMMdd_HH}.log" -f (Get-Date))
$LockFile = Join-Path $LogDir "pipeline_horario_ps.lock"

function Remove-HorarioPsLock {
    if (Test-Path $LockFile) {
        Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
    }
}

if (Test-Path $LockFile) {
    $lockAge = (Get-Item $LockFile).LastWriteTime
    $mins = [int]((Get-Date) - $lockAge).TotalMinutes
    if ($mins -lt 90) {
        Write-Host "Omitido: otra instancia pipeline horario en curso (${mins} min). Salida 0."
        exit 0
    }
    Write-Host "WARN: lock PS antiguo (${mins} min) - se reemplaza."
}
New-Item -Path $LockFile -ItemType File -Force | Out-Null

Write-Host "Pipeline horario - $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
Write-Host "Log: $LogFile"

$ScriptPy = Join-Path $Root "pipeline_horario.py"
try {
    $output = & $Py $ScriptPy 2>&1
    $code = $LASTEXITCODE
    $output | Tee-Object -FilePath $LogFile -Append
    exit $code
} finally {
    Remove-HorarioPsLock
}
