# Cuadrante Tatami: 4 corridas/día (America/Guayaquil, hora local del servidor).
#
#   mediodia   12:00  — cierre completo AYER (facturas, PAR, recalcular)
#   tarde      18:00  — operativo HOY (ventas + descargo pendientes + alertas)
#   medianoche 00:00  — operativo AYER recién cerrado (últimos tickets nocturnos)
#   seguridad  01:00  — nocturno AYER (reconciliar + descargo + recalcular + WA si falla)
#
# Uso manual:
#   .\ejecutar_pipeline_cuadrante.ps1 -Slot mediodia
#   .\ejecutar_pipeline_cuadrante.ps1 -Slot tarde
#
# Task Scheduler: 4 tareas diarias llamando este script con -Slot distinto.
# Registrar: .\registrar_cuadrante_tareas.ps1

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("mediodia", "tarde", "medianoche", "seguridad")]
    [string]$Slot
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"

$Py = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Error "No se encuentra venv en $Py"
    exit 1
}

$Now = Get-Date
$Hoy = $Now.ToString("yyyy-MM-dd")
$Ayer = $Now.AddDays(-1).ToString("yyyy-MM-dd")

$LogDir = Join-Path $Root "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}
$LogFile = Join-Path $LogDir "pipeline_cuadrante_${Slot}_${Hoy}_$($Now.ToString('HH')).log"

function Write-LogLine([string]$Msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

$PipelineArgs = @()
$Label = ""

switch ($Slot) {
    "mediodia" {
        $Label = "Cierre completo — ayer ($Ayer)"
        # Sin --fecha: pipeline_diario usa ayer calendario EC
        $PipelineArgs = @("$Root\pipeline_diario.py", "--modo", "completo")
    }
    "tarde" {
        $Label = "Operativo — hoy ($Hoy)"
        $PipelineArgs = @(
            "$Root\pipeline_diario.py",
            "--modo", "operativo",
            "--fecha", $Hoy
        )
    }
    "medianoche" {
        $Label = "Operativo — ayer ($Ayer) post-cierre"
        $PipelineArgs = @(
            "$Root\pipeline_diario.py",
            "--modo", "operativo",
            "--fecha", $Ayer
        )
    }
    "seguridad" {
        $Label = "Nocturno — cuadre ayer ($Ayer)"
        $PipelineArgs = @(
            "$Root\pipeline_diario.py",
            "--modo", "nocturno",
            "--fecha", $Ayer
        )
    }
}

Write-LogLine "========================================"
Write-LogLine "Cuadrante Tatami [$Slot] — $Label"
Write-LogLine "Log: $LogFile"
Write-LogLine "========================================"

$code = 1
try {
    & $Py @PipelineArgs 2>&1 | Tee-Object -FilePath $LogFile -Append
    $code = $LASTEXITCODE
} catch {
    Write-LogLine "ERROR PowerShell: $_"
    $code = 1
}

Write-LogLine "========================================"
Write-LogLine "Cuadrante [$Slot] — fin codigo_salida=$code"
Write-LogLine "========================================"
exit $code
