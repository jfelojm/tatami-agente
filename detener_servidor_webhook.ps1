# detener_servidor_webhook.ps1 — cierre forzado uvicorn + ngrok (puerto 8000)
# Ejecutar en PowerShell como administrador si algún PID no cede.

$ErrorActionPreference = "SilentlyContinue"

Write-Host "Deteniendo webhook Tatami..." -ForegroundColor Yellow

# 1) Procesos uvicorn / whatsapp_webhook
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match "uvicorn|whatsapp_webhook" } |
    ForEach-Object {
        Write-Host "  taskkill python PID $($_.ProcessId)"
        taskkill /F /PID $_.ProcessId | Out-Null
    }

# 2) Cualquier listener en 8000 (solo PID > 0)
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.OwningProcess -gt 0 } |
    ForEach-Object {
        Write-Host "  taskkill puerto 8000 PID $($_.OwningProcess)"
        taskkill /F /PID $_.OwningProcess | Out-Null
    }

# 3) ngrok del túnel local
Get-Process ngrok -ErrorAction SilentlyContinue |
    ForEach-Object {
        Write-Host "  taskkill ngrok PID $($_.Id)"
        taskkill /F /PID $_.Id | Out-Null
    }

Start-Sleep -Seconds 2

$libre = -not (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)
if ($libre) {
    Write-Host "Listo: puerto 8000 libre, uvicorn y ngrok detenidos." -ForegroundColor Green
} else {
    Write-Host "AVISO: puerto 8000 sigue ocupado. Reintenta este script como administrador." -ForegroundColor Red
}
