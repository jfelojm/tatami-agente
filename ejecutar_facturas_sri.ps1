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

$tag = if ($Corrida -match '^(AM|PM|MANUAL)$') { $Corrida } else { "MANUAL" }
$LogFile = Join-Path $LogDir ("facturas_sri_{0:yyyyMMdd}_{1}.log" -f (Get-Date), $tag)
$LockFile = Join-Path $LogDir "facturas_sri.lock"

function Remove-SriLock {
    if (Test-Path $LockFile) {
        Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
    }
}

if (Test-Path $LockFile) {
    $lockAge = (Get-Item $LockFile).LastWriteTime
    $mins = [int]((Get-Date) - $lockAge).TotalMinutes
    if ($mins -lt 120) {
        Write-Error "Otra corrida SRI en curso (lock desde hace ${mins} min). Espere o borre $LockFile si quedo colgada."
        exit 1
    }
    Write-Host "WARN: lock SRI antiguo (${mins} min) - se reemplaza."
}
New-Item -Path $LockFile -ItemType File -Force | Out-Null

try {
    Start-Transcript -Path $LogFile -Append -Encoding utf8 | Out-Null
} catch {
    Write-Host "WARN: no se pudo abrir transcript en $LogFile : $_"
}

Write-Host "========================================"
Write-Host "Facturas SRI Tatami -- $(Get-Date -Format 'yyyy-MM-dd HH:mm') | corrida $tag"
Write-Host "Log: $LogFile"
Write-Host "========================================"

# Aviso si no hay 2captcha (tareas AM/PM fallan sin humano en pantalla)
$envFile = Join-Path $Root ".env"
if (Test-Path $envFile) {
    $envText = Get-Content $envFile -Raw
    if ($envText -notmatch 'SRI_CAPTCHA_2CAPTCHA_KEY=[^\r\n#]') {
        Write-Host "AVISO: sin SRI_CAPTCHA_2CAPTCHA_KEY - debe resolver captcha en el navegador visible."
        Write-Host "       Las tareas TatamiFacturasSRI_AM/PM fallan si nadie esta en el PC."
    }
}

$pyArgs = @("-u", "$Root\procesar_facturas_sri.py")
if ($Corrida -match '^(AM|PM|MANUAL)$') {
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
    Remove-SriLock
    try { Stop-Transcript | Out-Null } catch { }
    exit 0
}
$code = 1
try {
    & $Py @pyArgs 2>&1 | Tee-Object -FilePath $LogFile -Append
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
} catch {
    Write-Host "ERROR ejecutando Python: $_"
    $code = 1
} finally {
    Remove-SriLock
    try { Stop-Transcript | Out-Null } catch { }
}
exit $code
