# Copia ALLOWLIST_STAFF_COCINA (y otras allowlists) al portapapeles para Railway
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$EnvPath = Join-Path $Root ".env"

if (-not (Test-Path $EnvPath)) {
    Write-Error "No se encuentra $EnvPath"
    exit 1
}

$vars = @(
    "ALLOWLIST_STAFF_COCINA",
    "ALLOWLIST_STAFF_BARRA",
    "ALLOWLIST_JEFE_COCINA",
    "ALLOWLIST_JEFE_BARRA",
    "ALLOWLIST_SOCIO"
)

$lines = @()
foreach ($name in $vars) {
    $m = Select-String -Path $EnvPath -Pattern "^$name=(.+)$" | Select-Object -First 1
    if ($m) {
        $val = $m.Matches[0].Groups[1].Value.Trim()
        $lines += "$name=$val"
    }
}

if (-not $lines) {
    Write-Error "No se encontraron allowlists en .env"
    exit 1
}

$text = $lines -join "`n"
$text | Set-Clipboard
Write-Host "OK: allowlists copiadas al portapapeles."
Write-Host ""
Write-Host "En Railway -> tu servicio tatami-agente -> Variables:"
Write-Host "  1. Edita ALLOWLIST_STAFF_COCINA (debe incluir 593993794670 para Charlie)"
Write-Host "  2. Save (reinicia el servicio)"
Write-Host ""
Write-Host "Valor ALLOWLIST_STAFF_COCINA:"
$staff = ($lines | Where-Object { $_ -like "ALLOWLIST_STAFF_COCINA=*" }) -replace "^ALLOWLIST_STAFF_COCINA=", ""
Write-Host $staff
