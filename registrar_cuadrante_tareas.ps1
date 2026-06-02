# Crea las 4 tareas del cuadrante en Task Scheduler (Windows).
#
# Configuración objetivo:
#   - Ejecutar aunque el usuario NO haya iniciado sesión (LogonType Password)
#   - Ejecutar con los privilegios más altos (RunLevel Highest)
#   - Permitir ejecución con batería (no detener al desconectar corriente)
#   - Iniciar si se perdió la hora programada (StartWhenAvailable)
#   - Despertar el equipo para ejecutar (WakeToRun)
#   - Solo si hay red (RunOnlyIfNetworkAvailable)
#
# Requisitos:
#   1. PowerShell como administrador
#   2. Contraseña de Windows del usuario que ejecutará la tarea
#
# Uso:
#   .\registrar_cuadrante_tareas.ps1
#
# Elimina la tarea horaria legacy TatamiPipelineHorario si existe.

#Requires -RunAsAdministrator

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script = Join-Path $Root "ejecutar_pipeline_cuadrante.ps1"
$PSExe = (Get-Command powershell.exe).Source
$DefaultUser = if ($env:USERDOMAIN -and $env:USERDOMAIN -ne $env:COMPUTERNAME) {
    "$env:USERDOMAIN\$env:USERNAME"
} else {
    $env:USERNAME
}

$Tasks = @(
    @{ Name = "TatamiCuadrante_Mediodia";   Time = "12:00"; Slot = "mediodia" },
    @{ Name = "TatamiCuadrante_Tarde";      Time = "18:00"; Slot = "tarde" },
    @{ Name = "TatamiCuadrante_Medianoche";  Time = "00:00"; Slot = "medianoche" },
    @{ Name = "TatamiCuadrante_Seguridad";   Time = "01:00"; Slot = "seguridad" }
)

Write-Host "Registrando cuadrante Tatami desde: $Root"
Write-Host ""
Write-Host "Seguridad: sin sesion iniciada + privilegios altos"
Write-Host "Condiciones: permite bateria, requiere red, recupera horario perdido, despierta el equipo"
Write-Host ""

$cred = Get-Credential -UserName $DefaultUser -Message "Contraseña Windows para tareas del cuadrante (sin sesión iniciada)"
if (-not $cred) {
    Write-Error "Registro cancelado: se necesita contraseña."
    exit 1
}
$plainPwd = $cred.GetNetworkCredential().Password

Unregister-ScheduledTask -TaskName "TatamiPipelineHorario" -Confirm:$false -ErrorAction SilentlyContinue
if ($?) {
    Write-Host "Eliminada tarea legacy TatamiPipelineHorario"
}

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)

foreach ($t in $Tasks) {
    Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null

    $Args = "-NoProfile -ExecutionPolicy Bypass -File `"$Script`" -Slot $($t.Slot)"
    $Action = New-ScheduledTaskAction -Execute $PSExe -Argument $Args -WorkingDirectory $Root
    $Trigger = New-ScheduledTaskTrigger -Daily -At $t.Time
    try {
        Register-ScheduledTask `
            -TaskName $t.Name `
            -Action $Action `
            -Trigger $Trigger `
            -Settings $Settings `
            -User $cred.UserName `
            -Password $plainPwd `
            -RunLevel Highest `
            -Force `
            -ErrorAction Stop | Out-Null
        Write-Host "OK  $($t.Name)  $($t.Time)  Slot=$($t.Slot)"
    } catch {
        Write-Host "ERR $($t.Name)  $($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host "Verificar:"
foreach ($t in $Tasks) {
    $task = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
    if ($task) {
        $info = Get-ScheduledTaskInfo -TaskName $t.Name
        $p = $task.Principal
        $s = $task.Settings
        Write-Host "  $($task.TaskName)"
        Write-Host "    Estado=$($task.State)  Proxima=$($info.NextRunTime)"
        Write-Host "    LogonType=$($p.LogonType)  RunLevel=$($p.RunLevel)  Usuario=$($p.UserId)"
        Write-Host "    Bateria OK=$([bool](-not $s.DisallowStartIfOnBatteries))  NoDetenerEnBateria=$([bool](-not $s.StopIfGoingOnBatteries))"
        Write-Host "    StartWhenAvailable=$($s.StartWhenAvailable)  WakeToRun=$($s.WakeToRun)  RedRequerida=$($s.RunOnlyIfNetworkAvailable)"
    } else {
        Write-Host "  $($t.Name)  NO ENCONTRADA"
    }
}
Write-Host ""
Write-Host "Requisito: PC encendida o en suspensión (WakeToRun) y red a Smart Menu en 12:00, 18:00, 00:00 y 01:00."
Write-Host "Nota: WakeToRun no aplica si la PC está apagada; en ese caso StartWhenAvailable corre al encender."
