# Estado de tareas Tatami (no requiere admin).
# Uso: .\verificar_tareas_tatami.ps1

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Activas = @(
    "TatamiPipelineHorario",
    "TatamiDigestMatutino",
    "TatamiPARSemanal",
    "TatamiServidorWebhook"
)
$Legacy = @(
    "TatamiCuadrante_Mediodia",
    "TatamiCuadrante_Tarde",
    "TatamiCuadrante_Medianoche",
    "TatamiCuadrante_Seguridad",
    "TatamiFacturasSRI_AM",
    "TatamiFacturasSRI_PM"
)

Write-Host "=== Tareas estrategia (activas) ==="
foreach ($n in $Activas) {
    $task = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "  $n | NO ENCONTRADA"
        continue
    }
    $info = Get-ScheduledTaskInfo -TaskName $n
    $ult = if ($info.LastRunTime.Year -gt 2000) { $info.LastRunTime.ToString("yyyy-MM-dd HH:mm") } else { "nunca" }
    $prox = if ($info.NextRunTime.Year -gt 2000) { $info.NextRunTime.ToString("yyyy-MM-dd HH:mm") } else { "N/A" }
    $res = switch ($info.LastTaskResult) { 0 { "OK" } 267011 { "sin corrida" } default { "codigo $($info.LastTaskResult)" } }
    Write-Host "  $n | $($task.State) | ultima: $ult ($res) | proxima: $prox"
    if ($n -eq "TatamiPipelineHorario" -and $task.Triggers.Count -gt 0) {
        $rep = $task.Triggers[0].Repetition
        if ($rep) {
            Write-Host "    repeticion: cada $($rep.Interval) durante $($rep.Duration)"
        }
    }
}

Write-Host ""
Write-Host "=== Legacy (deben estar Disabled) ==="
foreach ($n in $Legacy) {
    $task = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "  $n | no existe"
        continue
    }
    $info = Get-ScheduledTaskInfo -TaskName $n
    $ult = if ($info.LastRunTime.Year -gt 2000) { $info.LastRunTime.ToString("yyyy-MM-dd HH:mm") } else { "nunca" }
    Write-Host "  $n | $($task.State) | ultima: $ult"
}

$cp = Join-Path $Root "logs\pipeline_checkpoint.json"
$hcp = Join-Path $Root "logs\pipeline_horario_checkpoint.json"
Write-Host ""
Write-Host "=== Checkpoints ==="
if (Test-Path $cp) { Get-Content $cp -Raw }
if (Test-Path $hcp) { Get-Content $hcp -Raw }

Write-Host ""
Write-Host "Registrar/actualizar tareas (admin): .\registrar_estrategia_tareas.ps1"
