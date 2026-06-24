# Procesa XML de facturas en Google Drive (GOOGLE_DRIVE_FACTURAS_FOLDER_ID).
#
# Uso:
#   .\ejecutar_facturas_drive.ps1              # produccion
#   .\ejecutar_facturas_drive.ps1 -DryRun      # simulacion
#   .\ejecutar_facturas_drive.ps1 -Listar      # solo lista XML en carpeta
#   .\ejecutar_facturas_drive.ps1 -Auditar     # vs catalogo BD_ITEMS_PROV

param(
    [switch]$DryRun,
    [switch]$Listar,
    [switch]$Auditar,
    [switch]$Reprocesar
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Py = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Error "No se encuentra venv en $Py"
    exit 1
}

$env:PYTHONIOENCODING = "utf-8"

$LogDir = Join-Path $Root "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}
$LogFile = Join-Path $LogDir ("facturas_drive_{0:yyyyMMdd_HHmmss}.log" -f (Get-Date))

if ($Listar) {
    & $Py (Join-Path $Root "listar_facturas_drive.py")
    exit $LASTEXITCODE
}

if ($Auditar) {
    & $Py (Join-Path $Root "auditar_facturas_vs_catalogo.py") 2>&1 | Tee-Object -FilePath $LogFile
    exit $LASTEXITCODE
}

$pyArgs = @(Join-Path $Root "procesar_facturas_drive.py")
if ($DryRun) { $pyArgs += "--dry-run" }
if ($Reprocesar) { $pyArgs += "--reprocesar" }

Write-Host "========================================"
Write-Host "Facturas Drive Tatami - $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
Write-Host "Log: $LogFile"
Write-Host "========================================"

& $Py @pyArgs 2>&1 | Tee-Object -FilePath $LogFile
exit $LASTEXITCODE
