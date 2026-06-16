# PAR semanal domingo 20:00.

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"

$Py = Join-Path $Root "venv\Scripts\python.exe"
$LogDir = Join-Path $Root "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogFile = Join-Path $LogDir ("par_semanal_{0:yyyyMMdd}.log" -f (Get-Date))

Write-Host "PAR semanal - $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
$ScriptPy = Join-Path $Root "par_semanal.py"
$output = & $Py $ScriptPy 2>&1
$code = $LASTEXITCODE
$output | Tee-Object -FilePath $LogFile -Append
exit $code
