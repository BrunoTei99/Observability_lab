# ShopLab POC: observabilidade com ClickHouse, Prometheus, Grafana e Uptime Kuma

O guia completo, passo a passo, está no documento que acompanha este projeto.

## Arranque rápido (Windows 11 + Docker Desktop, PowerShell)

```powershell
cd $HOME\shoplab-poc
docker compose up -d --build
docker compose ps
```

## Endereços

| Serviço        | URL                                  | Credenciais           |
|----------------|--------------------------------------|-----------------------|
| Loja ShopLab   | http://localhost:8080                | -                     |
| Modo caos      | http://localhost:8080/admin          | -                     |
| Grafana        | http://localhost:3000                | admin / admin123      |
| Uptime Kuma    | http://localhost:3001                | admin / ShopLab2026!  |
| Página estado  | http://localhost:3001/status/shoplab | -                     |
| Prometheus     | http://localhost:9090                | -                     |
| Alertmanager   | http://localhost:9093                | -                     |
| ClickHouse     | http://localhost:8123/play           | admin / admin123      |

## Comandos úteis

```powershell
docker compose logs -f alert-logger      # ver alertas a chegar
docker compose logs -f simulator         # ver tráfego simulado
powershell -ExecutionPolicy Bypass -File .\scripts\chaos.ps1 -Mode errors
powershell -ExecutionPolicy Bypass -File .\scripts\status.ps1
docker compose down                      # parar (mantém dados)
docker compose down -v                   # parar e apagar tudo
```
