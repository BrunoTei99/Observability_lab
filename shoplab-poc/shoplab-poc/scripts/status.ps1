# Mostra o estado dos contentores e a saude da plataforma.
# Uso: powershell -ExecutionPolicy Bypass -File .\scripts\status.ps1
Write-Host "`n== Contentores ==" -ForegroundColor Cyan
docker compose ps --format "table {{.Name}}`t{{.State}}`t{{.Status}}"
Write-Host "`n== Saude da plataforma (ultimos 60s) ==" -ForegroundColor Cyan
curl.exe -s http://localhost:8080/health/platform
Write-Host "`n`n== Ligacao ao ClickHouse ==" -ForegroundColor Cyan
curl.exe -s http://localhost:8080/health
Write-Host "`n`n== Modo caos ==" -ForegroundColor Cyan
curl.exe -s http://localhost:8080/admin/chaos
Write-Host ""
