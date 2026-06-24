# Crea tareas diarias de descarga SRI (10:00 AM y 18:00 PM).
#
# Uso (PowerShell como administrador, con contrasena Windows):
#   .\registrar_facturas_sri_tareas.ps1
#
# Sin admin (solo si hay sesion iniciada a las 10:00/18:00):
#   .\registrar_facturas_sri_tareas.ps1 -SinAdmin
#
# Eliminar:
#   Unregister-ScheduledTask -TaskName TatamiFacturasSRI_AM,TatamiFacturasSRI_PM -Confirm:$false

param(
    [switch]$SinAdmin
)

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script = Join-Path $Root "ejecutar_facturas_sri.ps1"
$PSExe = (Get-Command powershell.exe).Source

$Tasks = @(
    @{ Name = "TatamiFacturasSRI_AM"; Time = "10:00"; Corrida = "AM" },
    @{ Name = "TatamiFacturasSRI_PM"; Time = "18:00"; Corrida = "PM" }
)

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Write-Host "Registrando facturas SRI desde: $Root"

$cred = $null
if (-not $SinAdmin) {
    try {
        $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if (-not $isAdmin) {
            throw "Se requiere PowerShell como administrador"
        }
        $DefaultUser = if ($env:USERDOMAIN -and $env:USERDOMAIN -ne $env:COMPUTERNAME) {
            "$env:USERDOMAIN\$env:USERNAME"
        } else {
            $env:USERNAME
        }
        $cred = Get-Credential -UserName $DefaultUser -Message "Contrasena Windows para tareas SRI (sin sesion)"
    } catch {
        Write-Host "WARN: sin credencial admin - use -SinAdmin para tarea interactiva"
    }
}

foreach ($t in $Tasks) {
    Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null

    $Args = "-NoProfile -ExecutionPolicy Bypass -File `"$Script`" $($t.Corrida)"
    $Action = New-ScheduledTaskAction -Execute $PSExe -Argument $Args -WorkingDirectory $Root
    $Trigger = New-ScheduledTaskTrigger -Daily -At $t.Time

    try {
        if ($cred) {
            $plainPwd = $cred.GetNetworkCredential().Password
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
        } else {
            # Interactive: Chrome visible requiere sesión Windows iniciada a las 10:00/18:00
            $Principal = New-ScheduledTaskPrincipal `
                -UserId $env:USERNAME `
                -LogonType Interactive `
                -RunLevel Highest
            Register-ScheduledTask `
                -TaskName $t.Name `
                -Action $Action `
                -Trigger $Trigger `
                -Settings $Settings `
                -Principal $Principal `
                -Force `
                -ErrorAction Stop | Out-Null
        }
        Write-Host "OK  $($t.Name)  $($t.Time)  corrida=$($t.Corrida)"
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
        Write-Host "  $($task.TaskName)  Estado=$($task.State)  Proxima=$($info.NextRunTime)"
    } else {
        Write-Host "  $($t.Name)  NO ENCONTRADA"
    }
}
