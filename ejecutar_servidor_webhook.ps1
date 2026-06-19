# ejecutar_servidor_webhook.ps1
# Por defecto BLOQUEADO: Meta debe usar Railway (código actualizado), no ngrok local.
# Solo forzar local:  $env:TATAMI_FORCE_LOCAL_WEBHOOK=1  . .\ejecutar_servidor_webhook.ps1
if (-not $env:TATAMI_FORCE_LOCAL_WEBHOOK) {
    Write-Host ""
    Write-Host "WEBHOOK LOCAL DESHABILITADO" -ForegroundColor Red
    Write-Host "  Meta debe apuntar a: https://tatami-agente-production.up.railway.app/webhook"
    Write-Host "  Si sigue ngrok, WhatsApp usa codigo viejo de esta PC."
    Write-Host "  Para detener local: .\detener_servidor_webhook.ps1"
    Write-Host "  Desactiva la tarea programada TatamiServidorWebhook (admin)."
    Write-Host ""
    exit 1
}

$Root = "C:\Users\Usuario\Desktop\Agente Tatami\tatami-agente"
Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"
$Py = Join-Path $Root "venv\Scripts\python.exe"
$LogDir = Join-Path $Root "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}
$LogFileOut = Join-Path $LogDir "webhook_stdout.log"
$LogFileErr = Join-Path $LogDir "webhook_stderr.log"

Start-Process -FilePath $Py `
  -ArgumentList "-u","-m","uvicorn","whatsapp_webhook:app","--host","0.0.0.0","--port","8000" `
  -WorkingDirectory $Root `
  -RedirectStandardOutput $LogFileOut `
  -RedirectStandardError $LogFileErr `
  -WindowStyle Hidden

Start-Sleep -Seconds 3

Start-Process -FilePath "ngrok" `
  -ArgumentList "http --domain=polish-vindicate-smudgy.ngrok-free.dev 8000" `
  -WorkingDirectory $Root `
  -WindowStyle Hidden
