"""Configura o Uptime Kuma automaticamente (corre uma vez e termina).

Cria a conta de administrador, liga o Docker, cria os monitores dos
contentores e da plataforma, a página de estado e uma API key para o
Prometheus. É idempotente: se voltar a correr, só cria o que faltar.
"""
import os
import time
from urllib.parse import quote

from uptime_kuma_api import DockerType, MonitorType, UptimeKumaApi

KUMA_URL = os.getenv("KUMA_URL", "http://uptime-kuma:3001")
USER = os.getenv("KUMA_USER", "admin")
PASSWORD = os.getenv("KUMA_PASSWORD", "ShopLab2026!")
SECRET_FILE = os.getenv("SECRET_FILE", "/secrets/uptime-kuma-api-key")
INTERVAL = 20

CONTAINERS = ["shoplab-webapp", "shoplab-simulator", "shoplab-clickhouse", "shoplab-prometheus",
              "shoplab-alertmanager", "shoplab-alert-logger", "shoplab-grafana"]

FRESH_SQL = ("SELECT if(count() > 0, 'FRESH', 'STALE') FROM platform.http_requests "
             "WHERE ts > now() - INTERVAL 1 MINUTE")
FRESH_URL = f"http://clickhouse:8123/?user=grafana&password=grafana123&query={quote(FRESH_SQL)}"

HTTP_MONITORS = [
    # (grupo da página de estado, nome, tipo, url, keyword)
    ("Plataforma ShopLab", "Loja (página inicial)", MonitorType.HTTP, "http://webapp:8000/", None),
    ("Plataforma ShopLab", "API e ligação ao ClickHouse", MonitorType.HTTP, "http://webapp:8000/health", None),
    ("Plataforma ShopLab", "Saúde da plataforma (erros e latência)", MonitorType.HTTP,
     "http://webapp:8000/health/platform", None),
    ("Plataforma ShopLab", "Logs a chegar ao ClickHouse", MonitorType.KEYWORD, FRESH_URL, "FRESH"),
    ("Observabilidade", "ClickHouse", MonitorType.HTTP, "http://clickhouse:8123/ping", None),
    ("Observabilidade", "Prometheus", MonitorType.HTTP, "http://prometheus:9090/-/healthy", None),
    ("Observabilidade", "Alertmanager", MonitorType.HTTP, "http://alertmanager:9093/-/healthy", None),
    ("Observabilidade", "Grafana", MonitorType.HTTP, "http://grafana:3000/api/health", None),
]


def connect():
    for attempt in range(60):
        try:
            api = UptimeKumaApi(KUMA_URL, timeout=30, wait_events=0.5)
            print(f"Ligado ao Uptime Kuma em {KUMA_URL}")
            return api
        except Exception as exc:  # noqa: BLE001
            print(f"Uptime Kuma ainda não disponível ({exc}); tentativa {attempt + 1}/60")
            time.sleep(5)
    raise SystemExit("Não foi possível ligar ao Uptime Kuma")


def main():
    api = connect()
    if api.need_setup():
        api.setup(USER, PASSWORD)
        print(f"Conta de administrador criada: {USER}")
    api.login(USER, PASSWORD)

    # Docker host (socket montado no contentor do Uptime Kuma)
    hosts = {h["name"]: h["id"] for h in api.get_docker_hosts()}
    if "Docker local" not in hosts:
        res = api.add_docker_host(name="Docker local", dockerType=DockerType.SOCKET,
                                  dockerDaemon="/var/run/docker.sock")
        hosts["Docker local"] = res["id"]
        print("Docker host adicionado")
    docker_host = hosts["Docker local"]

    existing = {m["name"]: m["id"] for m in api.get_monitors()}
    groups = {"Plataforma ShopLab": [], "Contentores": [], "Observabilidade": []}

    for name in CONTAINERS:
        label = f"Contentor {name.replace('shoplab-', '')}"
        if label not in existing:
            res = api.add_monitor(type=MonitorType.DOCKER, name=label, docker_container=name,
                                  docker_host=docker_host, interval=INTERVAL, retryInterval=INTERVAL, maxretries=1)
            existing[label] = res["monitorID"]
            print(f"Monitor criado: {label}")
        groups["Contentores"].append(existing[label])

    for group, name, mtype, url, keyword in HTTP_MONITORS:
        if name not in existing:
            kwargs = {"type": mtype, "name": name, "url": url, "interval": INTERVAL,
                      "retryInterval": INTERVAL, "maxretries": 1}
            if keyword:
                kwargs["keyword"] = keyword
            res = api.add_monitor(**kwargs)
            existing[name] = res["monitorID"]
            print(f"Monitor criado: {name}")
        groups[group].append(existing[name])

    # Página de estado pública
    slugs = {p["slug"] for p in api.get_status_pages()}
    if "shoplab" not in slugs:
        api.add_status_page("shoplab", "ShopLab · Estado da plataforma")
    api.save_status_page(
        "shoplab", title="ShopLab · Estado da plataforma",
        description="Estado dos serviços da plataforma ShopLab (POC de observabilidade)",
        publicGroupList=[{"name": g, "weight": i + 1, "monitorList": [{"id": mid} for mid in ids]}
                         for i, (g, ids) in enumerate(groups.items())])
    print("Página de estado pronta em /status/shoplab")

    # API key para o Prometheus ler /metrics
    if os.path.exists(SECRET_FILE) and open(SECRET_FILE).read().strip():
        print("API key para o Prometheus já existe, nada a fazer")
    else:
        res = api.add_api_key(name="prometheus", expires=None, active=True)
        with open(SECRET_FILE, "w") as f:
            f.write(res["key"])
        os.chmod(SECRET_FILE, 0o644)
        print("API key criada e gravada para o Prometheus")

    api.disconnect()
    print("Configuração do Uptime Kuma concluída.")


if __name__ == "__main__":
    main()
