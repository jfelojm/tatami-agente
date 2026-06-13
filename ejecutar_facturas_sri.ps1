# Descarga y procesa facturas recibidas del SRI (independiente del pipeline diario).
# Task Scheduler sugerido:
#   TatamiFacturasSRI_AM  -> diario 10:00  -> .\ejecutar_facturas_sri.ps1 AM
#   TatamiFacturasSRI_PM  -> diario 18:00  -> .\ejecutar_facturas_sri.ps1 PM
#
# Primera vez (login + captcha portal):
#   .\ejecutar_facturas_sri.ps1 --init-portal-session
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

$code = 1
try {
    & $Py @pyArgs
    $code = $LASTEXITCODE
} finally {
    try { Stop-Transcript | Out-Null } catch { }
}
exit $code
