#Requires -RunAsAdministrator
# Aplica el fix al task TatamiPipelineHorario SIN re-registrar todo:
#   1. Habilita StartWhenAvailable (para que recupere corridas si el trigger llego tarde)
#   2. Quita RunOnlyIfNetworkAvailable (evita que problemas de red bloqueen la tarea)
#   3. Resetea el trigger a 07:00 con repeticion cada hora durante 18 h
#
# Ejecutar como administrador:
#   .\arreglar_task_horario.ps1

$TaskName = "TatamiPipelineHorario"

Write-Host "Verificando task '$TaskName'..."
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop

Write-Host "Estado actual: $($task.State)"

# Nuevos settings: StartWhenAvailable=True, sin RunOnlyIfNetworkAvailable
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

# Nuevo trigger: Daily 07:00, repetir cada 1h durante 18h
$trigger = New-ScheduledTaskTrigger -Daily -At "07:00"
$interval = "PT1H"
$duration = "PT18H"
if ($null -eq $trigger.Repetition) {
    $trigger.Repetition = New-CimInstance -ClientOnly `
        -ClassName MSFT_TaskRepetitionPattern `
        -Namespace Root/Microsoft/Windows/TaskScheduler `
        -Property @{
            Interval          = $interval
            Duration          = $duration
            StopAtDurationEnd = $false
        }
} else {
    $trigger.Repetition.Interval          = $interval
    $trigger.Repetition.Duration          = $duration
    $trigger.Repetition.StopAtDurationEnd = $false
}

try {
    Set-ScheduledTask -TaskName $TaskName -Settings $settings -Trigger $trigger -ErrorAction Stop | Out-Null
    Write-Host "OK  Settings actualizados (StartWhenAvailable=True, sin RunOnlyIfNetworkAvailable)"
    Write-Host "OK  Trigger reseteado a 07:00 con repeticion cada 1h durante 18h"
} catch {
    Write-Host "ERR al actualizar: $($_.Exception.Message)"
    exit 1
}

# Verificar resultado
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ""
Write-Host "--- Verificacion ---"
Write-Host "Estado:          $($task.State)"
Write-Host "Proxima corrida: $($info.NextRunTime)"
Write-Host "Ultimo resultado: $($info.LastTaskResult)"
$updatedTask = Get-ScheduledTask -TaskName $TaskName
$rep = $updatedTask.Triggers[0].Repetition
if ($rep) {
    Write-Host "Repeticion:      cada $($rep.Interval) durante $($rep.Duration)"
} else {
    Write-Host "WARN: sin repeticion (ejecuta de nuevo)"
}

Write-Host ""
Write-Host "Fix aplicado. El pipeline correra automaticamente cuando llegue el proximo trigger."
Write-Host "Tolerancia Python: hasta 55 minutos despues del slot (configurado en pipeline_horario.py)"
