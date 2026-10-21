"""Simulador de tráfego da ShopLab.

Cria utilizadores virtuais que navegam, pesquisam, fazem login, adicionam ao
carrinho e compram. O número de utilizadores ativos sobe e desce em ondas
(como um dia real comprimido em poucos minutos). Quando o modo caos "attack"
está ativo, arranca também bots de força bruta contra o login.
"""
import asyncio
import math
import os
import random
import time
import uuid

import httpx

TARGET = os.getenv("TARGET", "http://webapp:8000")
USERS = int(os.getenv("USERS", "30"))                 # utilizadores virtuais no pico
WAVE_MINUTES = float(os.getenv("WAVE_MINUTES", "10"))  # duração de um ciclo de tráfego
CLIENT_TIMEOUT = float(os.getenv("CLIENT_TIMEOUT", "5"))
THINK_MIN, THINK_MAX = 0.3, 2.5

CATEGORIES = ["electronics", "books", "home", "sports", "fashion", "toys", "garden"]
SEARCH_TERMS = CATEGORIES + ["phone", "headphones", "lamp", "sneakers", "lego", "drill", "kindle"]
COUNTRIES = ["PT"] * 40 + ["ES"] * 15 + ["BR"] * 15 + ["FR"] * 10 + ["DE"] * 8 + ["UK"] * 7 + ["US"] * 5
DEVICES = ["mobile"] * 55 + ["desktop"] * 35 + ["tablet"] * 10
AGENTS = {
    "mobile": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) Safari/604.1",
    "desktop": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/129.0 Safari/537.36",
    "tablet": "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) Safari/604.1",
}

stats = {"requests": 0, "errors": 0, "timeouts": 0, "conn_errors": 0, "purchases": 0}
state = {"mode": "normal", "active": 0}


def active_target():
    """Utilizadores ativos agora: onda sinusoidal entre ~20% e 100% de USERS."""
    phase = (time.time() / 60) / WAVE_MINUTES * 2 * math.pi
    return max(1, round(USERS * (0.6 + 0.4 * math.sin(phase))))


class Session:
    def __init__(self, client):
        self.client = client
        self.device = random.choice(DEVICES)
        self.user_id = 0
        self.candidate_id = random.randint(1, 5000)
        self.headers = {
            "X-Session-Id": uuid.uuid4().hex[:16],
            "X-Country": random.choice(COUNTRIES),
            "X-Device": self.device,
            "User-Agent": AGENTS[self.device],
            "X-Forwarded-For": f"{random.randint(11, 220)}.{random.randint(0, 255)}."
                               f"{random.randint(0, 255)}.{random.randint(1, 254)}",
            "X-User-Id": "0",
        }
        self.cart = []

    async def req(self, method, path, **kw):
        stats["requests"] += 1
        try:
            r = await self.client.request(method, TARGET + path, headers=self.headers,
                                          timeout=CLIENT_TIMEOUT, **kw)
            if r.status_code >= 500:
                stats["errors"] += 1
            return r
        except httpx.TimeoutException:
            stats["timeouts"] += 1
        except httpx.TransportError:
            stats["conn_errors"] += 1
        return None

    async def think(self):
        await asyncio.sleep(random.uniform(THINK_MIN, THINK_MAX))

    async def login(self):
        username = f"user{self.candidate_id}"
        self.headers["X-User-Id"] = str(self.candidate_id)
        password = "secret" if random.random() > 0.06 else "secrte"   # 6% enganam-se
        r = await self.req("POST", "/api/login", json={"username": username, "password": password})
        if r is not None and r.status_code == 401:
            await self.think()
            r = await self.req("POST", "/api/login", json={"username": username, "password": "secret"})
        if r is not None and r.status_code == 200:
            self.user_id = self.candidate_id
        else:
            self.headers["X-User-Id"] = "0"

    async def browse(self, views):
        await self.req("GET", f"/api/products?category={random.choice(CATEGORIES)}")
        await self.think()
        for _ in range(views):
            pid = random.randint(1, 200) if random.random() > 0.04 else random.randint(900, 9999)
            r = await self.req("GET", f"/api/products/{pid}")
            if r is not None and r.status_code == 200 and random.random() < 0.35:
                self.cart.append(pid)
            await self.think()

    async def search(self, n):
        for _ in range(n):
            await self.req("GET", f"/api/search?q={random.choice(SEARCH_TERMS)}")
            await self.think()

    async def add_cart(self):
        for pid in self.cart[:3]:
            await self.req("POST", "/api/cart", json={"product_id": pid})
            await self.think()

    async def checkout(self):
        if not self.cart:
            self.cart = [random.randint(1, 200)]
            await self.add_cart()
        r = await self.req("POST", "/api/checkout", json={"items": self.cart[:3]})
        if r is not None and r.status_code in (500, 504) and random.random() < 0.5:
            await self.think()   # metade dos utilizadores tenta outra vez
            r = await self.req("POST", "/api/checkout", json={"items": self.cart[:3]})
        if r is not None and r.status_code == 200:
            stats["purchases"] += 1


async def run_session(client):
    s = Session(client)
    persona = random.choices(["browser", "buyer", "searcher", "newcomer"], weights=[45, 25, 20, 10])[0]
    await s.req("GET", "/")
    await s.think()
    if persona == "newcomer":
        await s.req("POST", "/api/signup", json={"username": f"user{s.candidate_id}"})
        await s.think()
    if persona != "browser" or random.random() < 0.3:
        await s.login()
        await s.think()
    if persona == "searcher":
        await s.search(random.randint(2, 4))
    await s.browse(random.randint(1, 5))
    if persona == "browser" and s.user_id == 0 and s.cart and random.random() < 0.3:
        await s.req("POST", "/api/cart", json={"product_id": s.cart[0]})   # anónimo → 401
    if s.user_id and s.cart:
        await s.add_cart()
    if persona == "buyer" and s.user_id:
        await s.checkout()
    if s.user_id and random.random() < 0.5:
        await s.req("POST", "/api/logout")


async def user_loop(idx, client):
    await asyncio.sleep(random.uniform(0, 5))
    while True:
        if idx >= active_target():
            await asyncio.sleep(3)
            continue
        state["active"] += 1
        try:
            await run_session(client)
        except Exception as exc:  # noqa: BLE001
            print(f"[user {idx}] erro inesperado: {exc!r}", flush=True)
        finally:
            state["active"] -= 1
        await asyncio.sleep(random.uniform(1, 4))


async def bot_loop(client):
    """Bot de força bruta: só corre enquanto o modo caos é 'attack'."""
    ip = f"185.220.{random.randint(0, 255)}.{random.randint(1, 254)}"
    headers = {"X-Session-Id": uuid.uuid4().hex[:16], "X-Country": random.choice(["RU", "CN", "KP"]),
               "X-Device": "desktop", "User-Agent": "python-requests/2.31", "X-Forwarded-For": ip}
    while True:
        if state["mode"] != "attack":
            await asyncio.sleep(5)
            continue
        stats["requests"] += 1
        try:
            if random.random() < 0.8:
                await client.post(TARGET + "/api/login", headers=headers, timeout=CLIENT_TIMEOUT,
                                  json={"username": f"admin{random.randint(1, 99)}", "password": "123456"})
            else:
                await client.get(TARGET + f"/api/products/{random.randint(1000, 99999)}", headers=headers,
                                 timeout=CLIENT_TIMEOUT)
        except httpx.HTTPError:
            pass
        await asyncio.sleep(random.uniform(0.05, 0.3))


async def watch_mode(client):
    while True:
        try:
            r = await client.get(TARGET + "/admin/chaos", timeout=3)
            mode = r.json().get("mode", "normal")
            if mode != state["mode"]:
                print(f"[simulator] modo caos: {state['mode']} -> {mode}", flush=True)
            state["mode"] = mode
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(5)


async def report():
    while True:
        await asyncio.sleep(30)
        print(f"[simulator] ativos={state['active']}/{active_target()} modo={state['mode']} "
              f"pedidos={stats['requests']} erros5xx={stats['errors']} timeouts_cliente={stats['timeouts']} "
              f"falhas_ligacao={stats['conn_errors']} compras={stats['purchases']}", flush=True)


async def wait_for_target(client):
    while True:
        try:
            r = await client.get(TARGET + "/health/live", timeout=3)
            if r.status_code == 200:
                print(f"[simulator] plataforma disponível em {TARGET}, a arrancar {USERS} utilizadores", flush=True)
                return
        except Exception:  # noqa: BLE001
            pass
        print("[simulator] à espera da plataforma...", flush=True)
        await asyncio.sleep(3)


async def main():
    limits = httpx.Limits(max_connections=USERS + 20, max_keepalive_connections=USERS)
    async with httpx.AsyncClient(limits=limits) as client:
        await wait_for_target(client)
        tasks = [user_loop(i, client) for i in range(USERS)]
        tasks += [bot_loop(client) for _ in range(6)]
        tasks += [watch_mode(client), report()]
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
