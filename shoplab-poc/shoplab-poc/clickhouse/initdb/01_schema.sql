-- Executado automaticamente no primeiro arranque do ClickHouse (volume de dados vazio).
CREATE DATABASE IF NOT EXISTS platform;

-- Access log: um registo por pedido HTTP feito à plataforma
CREATE TABLE IF NOT EXISTS platform.http_requests
(
    ts              DateTime64(3, 'UTC'),
    request_id      UUID,
    session_id      String,
    user_id         UInt32,                  -- 0 = visitante anónimo
    method          LowCardinality(String),
    route           LowCardinality(String),  -- rota "modelo", ex. /api/products/{product_id}
    path            String,                  -- caminho real, ex. /api/products/42
    status          UInt16,
    duration_ms     Float32,
    response_bytes  UInt32,
    client_ip       IPv4,
    country         LowCardinality(String),
    device          LowCardinality(String),
    user_agent      String,
    error_type      LowCardinality(String)   -- vazio quando não houve erro
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (route, ts)
TTL toDateTime(ts) + INTERVAL 7 DAY;

-- Ações dos utilizadores (eventos de negócio)
CREATE TABLE IF NOT EXISTS platform.user_events
(
    ts           DateTime64(3, 'UTC'),
    session_id   String,
    user_id      UInt32,
    event        LowCardinality(String),     -- login_success, view_product, add_to_cart, purchase, ...
    product_id   UInt32,
    category     LowCardinality(String),
    amount       Decimal(10, 2),
    search_term  String,
    country      LowCardinality(String),
    device       LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (event, ts)
TTL toDateTime(ts) + INTERVAL 7 DAY;

-- Log aplicacional (INFO / WARN / ERROR)
CREATE TABLE IF NOT EXISTS platform.app_logs
(
    ts          DateTime64(3, 'UTC'),
    level       LowCardinality(String),
    logger      LowCardinality(String),      -- auth, catalog, search, cart, checkout, payment, db, admin
    message     String,
    error_type  LowCardinality(String),
    route       LowCardinality(String),
    request_id  String,
    user_id     UInt32,
    session_id  String
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (level, ts)
TTL toDateTime(ts) + INTERVAL 7 DAY;

-- Utilizador da plataforma web (escreve e lê os seus dados)
CREATE USER IF NOT EXISTS shoplab_app IDENTIFIED BY 'app123';
GRANT SELECT, INSERT ON platform.* TO shoplab_app;

-- Utilizador só de leitura para o Grafana e o Uptime Kuma
CREATE USER IF NOT EXISTS grafana IDENTIFIED BY 'grafana123';
GRANT SELECT ON platform.* TO grafana;
GRANT SELECT ON system.* TO grafana;
