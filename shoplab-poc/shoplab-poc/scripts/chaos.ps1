# Muda o modo caos da ShopLab.
# Uso: powershell -ExecutionPolicy Bypass -File .\scripts\chaos.ps1 -Mode errors
# Modos: normal, errors, slow, timeouts, attack, outage
param(
    [ValidateSet("normal", "errors", "slow", "timeouts", "attack", "outage")]
    [string]$Mode = "normal"
)
$body = @{ mode = $Mode } | ConvertTo-Json
$r = Invoke-RestMethod -Method Post -Uri "http://localhost:8080/admin/chaos" -ContentType "application/json" -Body $body
Write-Host "Modo caos ativo: $($r.mode) - $($r.description)" -ForegroundColor Yellow
