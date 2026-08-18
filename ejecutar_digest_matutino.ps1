# Digest matutino 8:00 - costos + alertas inventario/delta/pedidos barra.

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"

$Py = Join-Path $Root "venv\Scripts\python.exe"
$LogDir = Join-Path $Root "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogFile = Join-Path $LogDir ("digest_matutino_{0:yyyyMMdd}.log" -f (Get-Date))
$LockFile = Join-Path $LogDir "digest_matutino_ps.lock"

function Remove-DigestPsLock {
    if (Test-Path $LockFile) {
        Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
    }
}

if (Test-Path $LockFile) {
    $lockAge = (Get-Item $LockFile).LastWriteTime
    $mins = [int]((Get-Date) - $lockAge).TotalMinutes
    if ($mins -lt 120) {
        Write-Host "Omitido: digest matutino ya en curso (${mins} min). Salida 0."
        exit 0
    }
    Write-Host "WARN: lock digest PS antiguo (${mins} min) - se reemplaza."
}
New-Item -Path $LockFile -ItemType File -Force | Out-Null

Write-Host "Digest matutino - $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
$ScriptPy = Join-Path $Root "digest_matutino.py"
try {
    $output = & $Py $ScriptPy 2>&1
    $code = $LASTEXITCODE
    $output | Tee-Object -FilePath $LogFile -Append
    exit $code
} finally {
    Remove-DigestPsLock
}
