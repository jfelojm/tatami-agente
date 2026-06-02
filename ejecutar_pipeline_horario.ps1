# DEPRECATED: usar ejecutar_pipeline_cuadrante.ps1 (4 slots/día).
# Redirige a modo operativo tarde como fallback si se invoca por error.

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Write-Warning "ejecutar_pipeline_horario.ps1 está obsoleto. Usa ejecutar_pipeline_cuadrante.ps1"
& (Join-Path $Root "ejecutar_pipeline_cuadrante.ps1") -Slot tarde
exit $LASTEXITCODE
