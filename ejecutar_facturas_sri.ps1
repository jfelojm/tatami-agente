# Descarga y procesa facturas recibidas del SRI.
# Chrome + manual (recomendado):
#   .\configurar_sri_chrome.ps1              # primera vez: login
#   .\configurar_sri_chrome.ps1 -Descargar   # descarga (usted clic CONSULTAR)
#
# Flujo automatico (SRI_CONSULTA_MODO=auto en .env):
#   TatamiFacturasSRI_AM 10:00 | TatamiFacturasSRI_PM 18:00
#   TatamiPipelineHorario -> solo --solo-proceso (PIPELINE_SRI_SOLO_PROCESO=1)
# Habilitar tareas (PowerShell como admin): .\habilitar_facturas_sri.ps1
# Ene-feb u otros meses: subir XML a Drive y usar sync_drive_xml_supabase.py
#
# Ver log en vivo:
#   Get-Content .\logs\facturas_sri_20260610_AM.log -Wait -Tail 40

param(
    [Parameter(Position = 0)]
    [string]$Corrida = "AM",

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$env:PYTHONIOENCODING = "utf-8"
$Py = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Error "No se encuentra venv en $Py"
    exit 1
}

$LogDir = Join-Path $Root "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

$tag = if ($Corrida -match '^(AM|PM)$') { $Corrida } else { "MANUAL" }
$LogFile = Join-Path $LogDir ("facturas_sri_{0:yyyyMMdd}_{1}.log" -f (Get-Date), $tag)

try {
    Start-Transcript -Path $LogFile -Append -Encoding utf8 | Out-Null
} catch { }

Write-Host "========================================"
Write-Host "Facturas SRI Tatami -- $(Get-Date -Format 'yyyy-MM-dd HH:mm') | corrida $tag"
Write-Host "Log: $LogFile"
Write-Host "========================================"

$pyArgs = @("$Root\procesar_facturas_sri.py")
if ($Corrida -match '^(AM|PM)$') {
    $pyArgs += @("--corrida", $Corrida)
} elseif ($Corrida -like '--*') {
    $pyArgs += $Corrida
}
if ($ExtraArgs) {
    $pyArgs += $ExtraArgs
}

$isInit = ($Corrida -eq '--init-portal-session') -or ($ExtraArgs -contains '--init-portal-session')
if ($isInit) {
    Write-Host "Abriendo ventana NUEVA (PowerShell + Chrome) para login SRI..."
    $cmd = "Set-Location -LiteralPath '$Root'; `$env:PYTHONIOENCODING='utf-8'; & '$Py' '$Root\procesar_facturas_sri.py' --init-portal-session"
    Start-Process powershell -ArgumentList @('-NoExit', '-ExecutionPolicy', 'Bypass', '-Command', $cmd)
    Write-Host "Listo: revise la ventana de PowerShell y Chrome que se acaban de abrir."
    try { Stop-Transcript | Out-Null } catch { }
    exit 0
}

$code = 1
try {
    & $Py @pyArgs
    $code = $LASTEXITCODE
} finally {
    try { Stop-Transcript | Out-Null } catch { }
}
exit $code
