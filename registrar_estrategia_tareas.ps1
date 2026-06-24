# Registra tareas Task Scheduler de la estrategia BD_CONFIG (horario + digest + PAR + SRI).
# Desactiva cuadrante legacy.
#
# Requiere PowerShell como administrador:
#   .\registrar_estrategia_tareas.ps1

#Requires -RunAsAdministrator

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PSExe = (Get-Command powershell.exe).Source

$DefaultUser = if ($env:USERDOMAIN -and $env:USERDOMAIN -ne $env:COMPUTERNAME) {
    "$env:USERDOMAIN\$env:USERNAME"
} else {
    $env:USERNAME
}

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Write-Host "Registrando estrategia Tatami desde: $Root"
Write-Host ""

& (Join-Path $Root "desactivar_schedulers_legacy.ps1")

$cred = Get-Credential -UserName $DefaultUser -Message "Contrasena Windows para tareas Tatami"
if (-not $cred) {
    Write-Error "Registro cancelado."
    exit 1
}
$plainPwd = $cred.GetNetworkCredential().Password

function Register-TatamiTask {
    param(
        [string]$Name,
        [string]$ScriptPath,
        [object]$Trigger
    )
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    $Args = "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""
    $Action = New-ScheduledTaskAction -Execute $PSExe -Argument $Args -WorkingDirectory $Root
    try {
        Register-ScheduledTask `
            -TaskName $Name `
            -Action $Action `
            -Trigger $Trigger `
            -Settings $Settings `
            -User $cred.UserName `
            -Password $plainPwd `
            -RunLevel Highest `
            -Force `
            -ErrorAction Stop | Out-Null
        Write-Host "OK  $Name"
    } catch {
        Write-Host "ERR $Name - $($_.Exception.Message)"
    }
}

function New-HourlyDailyTrigger {
    param(
        [string]$At = "07:00",
        [int]$IntervalHours = 1,
        [int]$DurationHours = 18
    )
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    $interval = "PT{0}H" -f $IntervalHours
    $duration = "PT{0}H" -f $DurationHours
    if ($null -eq $trigger.Repetition) {
        $trigger.Repetition = New-CimInstance -ClientOnly `
            -ClassName MSFT_TaskRepetitionPattern `
            -Namespace Root/Microsoft/Windows/TaskScheduler `
            -Property @{
                Interval            = $interval
                Duration            = $duration
                StopAtDurationEnd   = $false
            }
    } else {
        $trigger.Repetition.Interval = $interval
        $trigger.Repetition.Duration = $duration
        $trigger.Repetition.StopAtDurationEnd = $false
    }
    return $trigger
}

# Horario 7:00-00:00: cada hora durante 18 h
$TriggerHorario = New-HourlyDailyTrigger -At "07:00" -IntervalHours 1 -DurationHours 18
Register-TatamiTask `
    -Name "TatamiPipelineHorario" `
    -ScriptPath (Join-Path $Root "ejecutar_pipeline_horario.ps1") `
    -Trigger $TriggerHorario

Register-TatamiTask `
    -Name "TatamiDigestMatutino" `
    -ScriptPath (Join-Path $Root "ejecutar_digest_matutino.ps1") `
    -Trigger (New-ScheduledTaskTrigger -Daily -At "08:00")

Register-TatamiTask `
    -Name "TatamiPARSemanal" `
    -ScriptPath (Join-Path $Root "ejecutar_par_semanal.ps1") `
    -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "20:00")

Write-Host ""
Write-Host "Registrando descarga SRI AM/PM (10:00 y 18:00)..."
& (Join-Path $Root "registrar_facturas_sri_tareas.ps1") -SinAdmin:$false

Write-Host ""
Write-Host "Tareas activas:"
foreach ($n in @("TatamiPipelineHorario", "TatamiDigestMatutino", "TatamiPARSemanal", "TatamiFacturasSRI_AM", "TatamiFacturasSRI_PM")) {
    $task = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
    if ($task) {
        $info = Get-ScheduledTaskInfo -TaskName $n
        Write-Host "  $n | $($task.State) | proxima: $($info.NextRunTime)"
        if ($n -eq "TatamiPipelineHorario" -and $task.Triggers.Count -gt 0) {
            $rep = $task.Triggers[0].Repetition
            if ($rep) {
                Write-Host "    repeticion: cada $($rep.Interval) durante $($rep.Duration)"
            } else {
                Write-Host "    WARN: sin repeticion horaria (vuelve a ejecutar este script)"
            }
        }
    } else {
        Write-Host "  $n | NO ENCONTRADA"
    }
}
