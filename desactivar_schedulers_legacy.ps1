# Desactiva schedulers legacy (cuadrante + SRI AM/PM + pipeline horario viejo).
# Uso: .\desactivar_schedulers_legacy.ps1

$Legacy = @(
    "TatamiCuadrante_Mediodia",
    "TatamiCuadrante_Tarde",
    "TatamiCuadrante_Medianoche",
    "TatamiCuadrante_Seguridad",
    "TatamiFacturasSRI_AM",
    "TatamiFacturasSRI_PM"
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
