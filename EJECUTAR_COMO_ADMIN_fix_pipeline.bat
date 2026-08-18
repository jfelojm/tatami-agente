@echo off
:: Haz doble clic aqui para aplicar el fix al Task Scheduler.
:: Aparecera ventana UAC (administrador) - haz clic en Si.
powershell -NoProfile -Command "Start-Process powershell.exe -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"%~dp0arreglar_task_horario.ps1\"' -Verb RunAs -Wait"
pause
