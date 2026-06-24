# Desactiva schedulers legacy (cuadrante). SRI AM/PM se registran aparte.
# Uso: .\desactivar_schedulers_legacy.ps1

$Legacy = @(
    "TatamiCuadrante_Mediodia",
    "TatamiCuadrante_Tarde",
    "TatamiCuadrante_Medianoche",
    "TatamiCuadrante_Seguridad"
)

Write-Host "Desactivando tareas legacy..."
foreach ($name in $Legacy) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        Disable-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue | Out-Null
        Write-Host "  DISABLED $name"
    }
}

Write-Host "Listo (tareas no eliminadas; quedan deshabilitadas)."
