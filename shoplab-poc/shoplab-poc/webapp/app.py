"""ShopLab: plataforma web de e-commerce fictícia para o POC de observabilidade.

Cada pedido HTTP, ação de utilizador e erro é gravado no ClickHouse
(tabelas http_requests, user_events e app_logs). Expõe também métricas
Prometheus em /metrics e endpoints de saúde para o Uptime Kuma.
O "modo caos" (/admin) permite provocar incidentes de propósito.
"""
import asyncio
import ipaddress
import os
import random
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal

import clickhouse_connect
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.routing import Match

# ---------------------------------------------------------------- configuração
CH_HOST = os.getenv("CH_HOST", "clickhouse")
CH_PORT = int(os.getenv("CH_PORT", "8123"))
CH_USER = os.getenv("CH_USER", "shoplab_app")
CH_PASSWORD = os.getenv("CH_PASSWORD", "app123")
FLUSH_INTERVAL = float(os.getenv("FLUSH_INTERVAL", "1.0"))

# ---------------------------------------------------------------- catálogo
CATEGORIES = ["electronics", "books", "home", "sports", "fashion", "toys", "garden"]
_rnd = random.Random(42)
PRODUCTS = {
    pid: {
        "id": pid,
        "name": f"Produto {pid}",
        "category": _rnd.choice(CATEGORIES),
        "price": round(_rnd.uniform(4.99, 499.99), 2),
    }
    for pid in range(1, 201)
}

# ---------------------------------------------------------------- modo caos
CHAOS_MODES = {
    "normal":   {"desc": "Funcionamento normal, poucos erros",
                 "error_rate": 0.01, "latency": (0, 0), "search_slow": 0.04,
                 "gateway_timeout": 0.03, "decline_rate": 0.06, "db_down": False},
    "errors":   {"desc": "Muitos erros 500 em toda a plataforma",
                 "error_rate": 0.25, "latency": (0, 0), "search_slow": 0.04,
                 "gateway_timeout": 0.03, "decline_rate": 0.06, "db_down": False},
    "slow":     {"desc": "Plataforma lenta (latência alta em todos os pedidos)",
                 "error_rate": 0.01, "latency": (600, 2500), "search_slow": 0.5,
                 "gateway_timeout": 0.1, "decline_rate": 0.06, "db_down": False},
    "timeouts": {"desc": "Gateway de pagamento a dar timeout",
                 "error_rate": 0.01, "latency": (0, 0), "search_slow": 0.04,
                 "gateway_timeout": 0.7, "decline_rate": 0.06, "db_down": False},
    "attack":   {"desc": "Ataque de força bruta ao login (bots)",
                 "error_rate": 0.01, "latency": (0, 0), "search_slow": 0.04,
                 "gateway_timeout": 0.03, "decline_rate": 0.06, "db_down": False},
    "outage":   {"desc": "Base de dados em baixo: a API inteira falha",
                 "error_rate": 0.01, "latency": (0, 0), "search_slow": 0.04,
                 "gateway_timeout": 0.03, "decline_rate": 0.06, "db_down": True},
}
chaos = {"mode": "normal", **CHAOS_MODES["normal"]}

INTERNAL_ERRORS = [
    ("db_error", "db", "Deadlock detetado ao atualizar o inventário"),
    ("db_error", "db", "Connection pool exhausted (max 20 ligações)"),
    ("internal_error", "app", "KeyError: 'price' ao serializar produto"),
    ("internal_error", "app", "TypeError: NoneType object is not subscriptable"),
    ("cache_error", "cache", "Redis timeout ao ler sessão"),
]

# ---------------------------------------------------------------- métricas Prometheus
HTTP_REQUESTS = Counter("http_requests_total", "Pedidos HTTP", ["method", "route", "status"])
HTTP_LATENCY = Histogram("http_request_duration_seconds", "Duração dos pedidos HTTP", ["route"],
                         buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10))
USER_EVENTS = Counter("user_events_total", "Ações de utilizadores", ["event"])
APP_ERRORS = Counter("app_errors_total", "Erros aplicacionais", ["error_type"])
CHAOS_GAUGE = Gauge("shoplab_chaos_mode", "Modo caos ativo (1 = ativo)", ["mode"])
for _m in CHAOS_MODES:
    CHAOS_GAUGE.labels(_m).set(1 if _m == "normal" else 0)

# ---------------------------------------------------------------- escrita no ClickHouse
TABLES = {
    "http_requests": ["ts", "request_id", "session_id", "user_id", "method", "route", "path", "status",
                      "duration_ms", "response_bytes", "client_ip", "country", "device", "user_agent",
                      "error_type"],
    "user_events": ["ts", "session_id", "user_id", "event", "product_id", "category", "amount",
                    "search_term", "country", "device"],
    "app_logs": ["ts", "level", "logger", "message", "error_type", "route", "request_id", "user_id",
                 "session_id"],
}
MAX_BUFFER = 100_000


class ClickHouseWriter(threading.Thread):
    """Acumula linhas em memória e grava-as em lote a cada FLUSH_INTERVAL segundos."""

    def __init__(self):
        super().__init__(daemon=True)
        self.buffers = {t: [] for t in TABLES}
        self.lock = threading.Lock()
        self.client = None
        self.healthy = False
        self.last_error = ""

    def add(self, table, row):
        with self.lock:
            buf = self.buffers[table]
            if len(buf) < MAX_BUFFER:
                buf.append(row)

    def _connect(self):
        self.client = clickhouse_connect.get_client(
            host=CH_HOST, port=CH_PORT, username=CH_USER, password=CH_PASSWORD,
            connect_timeout=3, send_receive_timeout=10,
        )

    def run(self):
        while True:
            time.sleep(FLUSH_INTERVAL)
            try:
                if self.client is None:
                    self._connect()
                with self.lock:
                    pending = {t: b for t, b in self.buffers.items() if b}
                    self.buffers = {t: [] for t in TABLES}
                for table, rows in pending.items():
                    try:
                        self.client.insert(f"platform.{table}", rows, column_names=TABLES[table])
                    except Exception:
                        with self.lock:  # devolve ao buffer para tentar de novo
                            self.buffers[table] = (rows + self.buffers[table])[:MAX_BUFFER]
                        raise
                if not pending:
                    self.client.command("SELECT 1")
                self.healthy, self.last_error = True, ""
            except Exception as exc:  # noqa: BLE001
                self.healthy, self.last_error = False, str(exc)[:200]
                self.client = None
                print(f"[clickhouse-writer] falha: {self.last_error}", flush=True)


writer = ClickHouseWriter()

# janela deslizante para /health/platform: (timestamp, status, duração_ms)
WINDOW = deque(maxlen=20_000)


def now_utc():
    return datetime.now(timezone.utc)


def ctx(request: Request):
    h = request.headers
    try:
        uid = int(h.get("x-user-id", "0"))
    except ValueError:
        uid = 0
    return {
        "user_id": uid,
        "session_id": h.get("x-session-id", ""),
        "country": h.get("x-country", "PT"),
        "device": h.get("x-device", "desktop"),
        "user_agent": h.get("user-agent", ""),
        "client_ip": _ipv4(h.get("x-forwarded-for") or (request.client.host if request.client else "")),
    }


def _ipv4(value):
    try:
        return str(ipaddress.IPv4Address(value.split(",")[0].strip()))
    except (ipaddress.AddressValueError, ValueError):
        return "0.0.0.0"


def log(request, level, logger, message, error_type="", route=""):
    c = ctx(request) if request is not None else {"user_id": 0, "session_id": ""}
    rid = getattr(request.state, "request_id", "") if request is not None else ""
    writer.add("app_logs", [now_utc(), level, logger, message, error_type, route, rid,
                            c["user_id"], c["session_id"]])


def event(request, name, product_id=0, category="", amount=0.0, search_term=""):
    c = ctx(request)
    writer.add("user_events", [now_utc(), c["session_id"], c["user_id"], name, product_id, category,
                               Decimal(str(round(amount, 2))), search_term, c["country"], c["device"]])
    USER_EVENTS.labels(name).inc()


class AppError(Exception):
    def __init__(self, status, error_type, message, logger, level=None):
        self.status, self.error_type, self.message, self.logger = status, error_type, message, logger
        self.level = level or ("ERROR" if status >= 500 else "WARN")


def maybe_internal_error():
    if random.random() < chaos["error_rate"]:
        etype, logger, msg = random.choice(INTERNAL_ERRORS)
        raise AppError(500, etype, msg, logger)


def require_user(request):
    if ctx(request)["user_id"] == 0:
        raise AppError(401, "not_authenticated", "Ação requer sessão iniciada", "auth")


app = FastAPI(title="ShopLab")


@app.on_event("startup")
def startup():
    writer.start()


def route_of(request: Request):
    for r in app.routes:
        match, _ = r.matches(request.scope)
        if match == Match.FULL:
            return getattr(r, "path", request.url.path)
    return "unmatched"


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    log(request, exc.level, exc.logger, exc.message, exc.error_type, route_of(request))
    return JSONResponse({"error": exc.error_type, "message": exc.message}, status_code=exc.status,
                        headers={"x-error-type": exc.error_type})


SKIP_PREFIXES = ("/health", "/metrics", "/admin", "/favicon")


@app.middleware("http")
async def observe(request: Request, call_next):
    path = request.url.path
    # endpoints técnicos e verificações do Uptime Kuma não contam como tráfego de utilizadores
    if path.startswith(SKIP_PREFIXES) or request.headers.get("user-agent", "").startswith("Uptime-Kuma"):
        return await call_next(request)

    request.state.request_id = str(uuid.uuid4())
    start = time.perf_counter()
    route = route_of(request)
    try:
        lo, hi = chaos["latency"]
        if hi:
            await asyncio.sleep(random.uniform(lo, hi) / 1000)
        if chaos["db_down"] and path.startswith("/api"):
            msg = "Ligação à base de dados recusada (connection refused 10.0.3.12:5432)"
            log(request, "ERROR", "db", msg, "db_unavailable", route)
            response = JSONResponse({"error": "db_unavailable", "message": msg}, status_code=503,
                                    headers={"x-error-type": "db_unavailable"})
        else:
            response = await call_next(request)
    except Exception as exc:  # noqa: BLE001  erro não tratado
        log(request, "ERROR", "app", f"Exceção não tratada: {exc!r}", "unhandled_exception", route)
        response = JSONResponse({"error": "unhandled_exception"}, status_code=500,
                                headers={"x-error-type": "unhandled_exception"})

    duration_ms = (time.perf_counter() - start) * 1000
    status = response.status_code
    error_type = response.headers.get("x-error-type", "")
    c = ctx(request)
    writer.add("http_requests", [
        now_utc(), request.state.request_id, c["session_id"], c["user_id"], request.method, route,
        path, status, duration_ms, int(response.headers.get("content-length", 0) or 0),
        c["client_ip"], c["country"], c["device"], c["user_agent"], error_type,
    ])
    HTTP_REQUESTS.labels(request.method, route, str(status)).inc()
    HTTP_LATENCY.labels(route).observe(duration_ms / 1000)
    if error_type:
        APP_ERRORS.labels(error_type).inc()
    WINDOW.append((time.time(), status, duration_ms))
    return response


# ---------------------------------------------------------------- páginas e API
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    maybe_internal_error()
    items = "".join(
        f"<li>{p['name']} <small>({p['category']})</small> <b>{p['price']:.2f} €</b></li>"
        for p in list(PRODUCTS.values())[:12]
    )
    return f"""<!doctype html><html lang="pt"><head><meta charset="utf-8"><title>ShopLab</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:system-ui,sans-serif;max-width:720px;margin:2rem auto;padding:0 1rem;color:#1f2937}}
li{{padding:.3rem 0}} small{{color:#6b7280}} a{{color:#2563eb}}</style></head>
<body><h1>ShopLab</h1><p>Loja de demonstração do POC de observabilidade.
Controlo de incidentes em <a href="/admin">/admin</a>.</p><h2>Destaques</h2><ul>{items}</ul></body></html>"""


@app.post("/api/signup")
async def signup(request: Request):
    maybe_internal_error()
    event(request, "signup")
    log(request, "INFO", "auth", "Nova conta criada", route="/api/signup")
    return {"ok": True}


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    maybe_internal_error()
    if body.get("password") != "secret":
        event(request, "login_failed")
        raise AppError(401, "invalid_credentials",
                       f"Login falhado para '{body.get('username', '?')}' (password errada)", "auth")
    event(request, "login_success")
    log(request, "INFO", "auth", f"Sessão iniciada por '{body.get('username')}'", route="/api/login")
    return {"ok": True, "user_id": ctx(request)["user_id"]}


@app.post("/api/logout")
async def logout(request: Request):
    event(request, "logout")
    return {"ok": True}


@app.get("/api/products")
async def list_products(request: Request, category: str = "", page: int = 1):
    maybe_internal_error()
    await asyncio.sleep(random.uniform(0.01, 0.06))
    items = [p for p in PRODUCTS.values() if not category or p["category"] == category]
    event(request, "browse_category", category=category)
    return {"items": items[(page - 1) * 20: page * 20], "total": len(items)}


@app.get("/api/products/{product_id}")
async def get_product(request: Request, product_id: int):
    maybe_internal_error()
    await asyncio.sleep(random.uniform(0.005, 0.04))
    p = PRODUCTS.get(product_id)
    if not p:
        raise AppError(404, "not_found", f"Produto {product_id} não existe", "catalog")
    event(request, "view_product", product_id=product_id, category=p["category"])
    return p


@app.get("/api/search")
async def search(request: Request, q: str = ""):
    maybe_internal_error()
    if random.random() < chaos["search_slow"]:
        delay = random.uniform(1.5, 4.5)
        if delay > 3.0:
            await asyncio.sleep(3.0)
            raise AppError(504, "query_timeout",
                           f"Pesquisa por '{q}' excedeu o limite de 3s no motor de pesquisa", "search")
        await asyncio.sleep(delay)
    else:
        await asyncio.sleep(random.uniform(0.03, 0.2))
    results = [p for p in PRODUCTS.values() if q and q in p["category"]][:20]
    event(request, "search", search_term=q)
    if not results:
        log(request, "INFO", "search", f"Pesquisa sem resultados: '{q}'", route="/api/search")
    return {"query": q, "results": results}


@app.post("/api/cart")
async def add_to_cart(request: Request):
    body = await request.json()
    require_user(request)
    maybe_internal_error()
    pid = int(body.get("product_id", 0))
    p = PRODUCTS.get(pid)
    if not p:
        raise AppError(404, "not_found", f"Produto {pid} não existe (carrinho)", "cart")
    await asyncio.sleep(random.uniform(0.01, 0.05))
    event(request, "add_to_cart", product_id=pid, category=p["category"], amount=p["price"])
    return {"ok": True}


async def payment_gateway(amount):
    """Gateway de pagamento externo simulado."""
    if random.random() < chaos["gateway_timeout"]:
        await asyncio.sleep(random.uniform(3, 6))
    else:
        await asyncio.sleep(random.uniform(0.15, 0.6))
    return random.random() >= chaos["decline_rate"]


@app.post("/api/checkout")
async def checkout(request: Request):
    body = await request.json()
    require_user(request)
    ids = [int(i) for i in body.get("items", []) if int(i) in PRODUCTS]
    if not ids:
        raise AppError(400, "validation_error", "Checkout com carrinho vazio", "checkout")
    total = sum(PRODUCTS[i]["price"] for i in ids)
    event(request, "checkout_started", amount=total)
    maybe_internal_error()
    try:
        approved = await asyncio.wait_for(payment_gateway(total), timeout=2.5)
    except asyncio.TimeoutError:
        event(request, "payment_failed", amount=total)
        raise AppError(504, "upstream_timeout",
                       f"Gateway de pagamento não respondeu em 2.5s (encomenda de {total:.2f} €)", "payment")
    if not approved:
        event(request, "payment_failed", amount=total)
        raise AppError(402, "payment_declined", f"Pagamento recusado pelo banco ({total:.2f} €)", "payment")
    order_id = uuid.uuid4().hex[:8]
    for i in ids:
        event(request, "purchase", product_id=i, category=PRODUCTS[i]["category"], amount=PRODUCTS[i]["price"])
    log(request, "INFO", "checkout", f"Encomenda {order_id} concluída: {len(ids)} artigos, {total:.2f} €",
        route="/api/checkout")
    return {"ok": True, "order_id": order_id, "total": round(total, 2)}


# ---------------------------------------------------------------- saúde e métricas
@app.get("/health/live")
async def health_live():
    return {"status": "alive"}


@app.get("/health")
async def health():
    body = {"status": "ok" if writer.healthy else "down",
            "clickhouse": "ok" if writer.healthy else f"down: {writer.last_error}"}
    return JSONResponse(body, status_code=200 if writer.healthy else 503)


@app.get("/health/platform")
async def health_platform():
    cutoff = time.time() - 60
    recent = [(s, d) for t, s, d in list(WINDOW) if t >= cutoff]
    n = len(recent)
    if n == 0:
        return {"status": "idle", "requests_last_60s": 0}
    errors = sum(1 for s, _ in recent if s >= 500)
    durations = sorted(d for _, d in recent)
    p95 = durations[min(n - 1, int(n * 0.95))]
    error_rate = errors / n
    problems = []
    if error_rate > 0.10:
        problems.append(f"taxa de erros {error_rate:.0%} > 10%")
    if p95 > 2000:
        problems.append(f"latência p95 {p95:.0f} ms > 2000 ms")
    body = {"status": "degraded" if problems else "healthy", "problems": problems,
            "requests_last_60s": n, "error_rate": round(error_rate, 4), "p95_ms": round(p95, 1),
            "chaos_mode": chaos["mode"]}
    return JSONResponse(body, status_code=503 if problems else 200)


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------- modo caos
@app.get("/admin/chaos")
async def get_chaos():
    return {"mode": chaos["mode"], "description": chaos["desc"],
            "modes": {k: v["desc"] for k, v in CHAOS_MODES.items()}}


@app.post("/admin/chaos")
async def set_chaos(request: Request):
    body = await request.json()
    mode = body.get("mode", "normal")
    if mode not in CHAOS_MODES:
        return JSONResponse({"error": f"modo desconhecido: {mode}"}, status_code=400)
    previous = chaos["mode"]
    chaos.clear()
    chaos.update({"mode": mode, **CHAOS_MODES[mode]})
    for m in CHAOS_MODES:
        CHAOS_GAUGE.labels(m).set(1 if m == mode else 0)
    log(request, "WARN", "admin", f"Modo caos alterado: {previous} → {mode}", route="/admin/chaos")
    print(f"[chaos] {previous} -> {mode}", flush=True)
    return {"mode": mode, "description": chaos["desc"]}


ADMIN_HTML = """<!doctype html><html lang="pt"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>ShopLab · Modo caos</title>
<style>
:root{color-scheme:light dark;--bg:#f8fafc;--card:#fff;--text:#0f172a;--muted:#64748b;--line:#e2e8f0;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#0b1120;--card:#111827;--text:#e5e7eb;--muted:#94a3b8;--line:#1f2937;--accent:#60a5fa}}
body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);max-width:760px;margin:2rem auto;padding:0 1rem}
h1{margin-bottom:.2rem} p.sub{color:var(--muted);margin-top:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:.8rem;margin-top:1.2rem}
button{all:unset;cursor:pointer;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.9rem 1rem}
button b{display:block;font-size:1.05rem;margin-bottom:.25rem} button span{color:var(--muted);font-size:.9rem}
button.active{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent) inset}
#status{margin-top:1.2rem;color:var(--muted)}
</style></head><body>
<h1>Modo caos</h1><p class="sub">Escolhe um cenário para provocar incidentes na ShopLab e vê a reação no Grafana, Uptime Kuma e alertas.</p>
<div class="grid" id="grid"></div><p id="status">A carregar…</p>
<script>
async function load(){
  const r = await fetch('/admin/chaos'); const d = await r.json();
  const g = document.getElementById('grid'); g.innerHTML='';
  for (const [k,v] of Object.entries(d.modes)){
    const b=document.createElement('button'); if(k===d.mode) b.className='active';
    b.innerHTML='<b>'+k+'</b><span>'+v+'</span>'; b.onclick=()=>setMode(k); g.appendChild(b);
  }
  document.getElementById('status').textContent='Modo ativo: '+d.mode+' — '+d.description;
}
async function setMode(m){
  await fetch('/admin/chaos',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:m})});
  load();
}
load();
</script></body></html>"""


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return ADMIN_HTML
