# Como funciona o POC ShopLab: plataforma, simulação, monitorização e alertas

Oct 21, 2026 · @Bruno

## O percurso de um pedido

Um único clique de um utilizador simulado atravessa toda a stack e pode acabar num alerta em cerca de 1 a 2 minutos. Este documento explica como cada peça desse percurso está montada e como criar à mão o que o POC configura automaticamente (dashboards, alertas e monitores).

O percurso de um pedido, do princípio ao fim:

1. **Simulador:** um utilizador virtual pede, por exemplo, `POST /api/checkout` à webapp.
2. **Webapp:** responde. Pelo caminho, um middleware mede a duração, apanha o erro (se houver) e regista tudo.
3. **ClickHouse:** a cada segundo, a webapp grava em lote os pedidos em `http_requests`, as ações em `user_events` e os erros em `app_logs`.
4. **Métricas:** ao mesmo tempo, a webapp incrementa contadores em memória, expostos em `/metrics`.
5. **Prometheus:** a cada 10 segundos lê esse `/metrics` (e o do ClickHouse e o do Uptime Kuma), guarda os valores e avalia as regras de alerta.
6. **Alertmanager:** quando uma regra fica verdadeira tempo suficiente, recebe o alerta e entrega-o ao alert-logger.
7. **Grafana:** lê o ClickHouse por SQL e o Prometheus por PromQL, e desenha os dashboards.
8. **Uptime Kuma:** em paralelo, testa a webapp e os contentores de fora, a cada 20 segundos, sem depender de nada do que está acima.

Há portanto dois tipos de dados a correr lado a lado. Os **logs e eventos** (no ClickHouse) guardam cada pedido individual, com todo o detalhe, e servem para investigar. As **métricas** (no Prometheus) são contadores agregados, leves e rápidos de consultar, e servem para alertar.

## A plataforma ShopLab

A webapp é uma loja em Python/FastAPI (`webapp/app.py`) que produz três saídas de observabilidade: logs no ClickHouse, métricas em `/metrics` e estado em `/health`. Tudo passa por um único ponto, um middleware que embrulha cada pedido.

### Os endpoints da loja

| Endpoint                 | O que faz                                       | Erros que pode dar                                                                     |
| ------------------------ | ----------------------------------------------- | -------------------------------------------------------------------------------------- |
| `GET /`                  | Página inicial                                  | 500 aleatório                                                                          |
| `POST /api/signup`       | Cria conta                                      | 500 aleatório                                                                          |
| `POST /api/login`        | Inicia sessão (a password certa é `secret`)     | 401 `invalid_credentials`                                                              |
| `GET /api/products`      | Lista produtos por categoria                    | 500 aleatório                                                                          |
| `GET /api/products/{id}` | Detalhe de um produto (existem os IDs 1 a 200)  | 404 `not_found`                                                                        |
| `GET /api/search`        | Pesquisa                                        | 504 `query_timeout` se demorar mais de 3 s                                             |
| `POST /api/cart`         | Adiciona ao carrinho (exige sessão)             | 401 `not_authenticated`, 404                                                           |
| `POST /api/checkout`     | Paga, chamando um gateway de pagamento simulado | 504 `upstream_timeout` (gateway acima de 2,5 s), 402 `payment_declined` (6%), 400, 500 |

Os "500 aleatórios" são tirados de uma lista de erros realistas (deadlock no inventário, pool de ligações esgotado, `KeyError`, timeout do Redis), com tipo `internal_error`, `db_error` ou `cache_error`. A probabilidade depende do modo caos.

### Como cada pedido é registado

O middleware `observe` faz isto a cada pedido:

1. Ignora os pedidos técnicos (`/health`, `/metrics`, `/admin`) e as verificações do Uptime Kuma (reconhecidas pelo User-Agent `Uptime-Kuma`), para não contarem como tráfego de utilizadores.
2. Gera um `request_id` e arranca o cronómetro.
3. Aplica o modo caos: latência extra ou, no modo `outage`, devolve logo 503.
4. Passa o pedido ao endpoint. Se o endpoint lançar um `AppError`, um handler transforma-o numa resposta JSON com o código certo, grava uma linha em `app_logs` (WARN para 4xx, ERROR para 5xx) e marca a resposta com o cabeçalho interno `x-error-type`. Uma exceção inesperada vira 500 `unhandled_exception`.
5. No fim, junta tudo numa linha para `http_requests`: hora, rota modelo (`/api/products/{product_id}`), caminho real, código, duração, IP, país, dispositivo, User-Agent e tipo de erro.
6. Atualiza as métricas Prometheus e a janela de 60 segundos usada pelo `/health/platform`.

Os endpoints, por seu lado, gravam as ações de negócio em `user_events` (`login_success`, `view_product`, `add_to_cart`, `checkout_started`, `purchase`, etc.) e algumas linhas INFO em `app_logs` ("Encomenda concluída", "Pesquisa sem resultados").

O país, o dispositivo, o utilizador e a sessão vêm de cabeçalhos que o simulador envia (`X-Country`, `X-Device`, `X-User-Id`, `X-Session-Id`, `X-Forwarded-For`). Numa plataforma real viriam da sessão e de um serviço de geolocalização por IP.

### Como os dados chegam ao ClickHouse

Gravar linha a linha seria lento e criaria demasiadas parts no ClickHouse. Por isso, a classe `ClickHouseWriter` corre numa thread separada: os pedidos só acrescentam linhas a buffers em memória, e a thread grava cada buffer num único `INSERT` a cada segundo. Se o ClickHouse estiver em baixo, as linhas voltam para o buffer (até 100 000 por tabela) e são gravadas quando ele regressar. O resultado de cada tentativa define se o `/health` responde 200 ou 503.

### Métricas expostas em /metrics

| Métrica | Tipo | Etiquetas | Mede |
| --- | --- | --- | --- |
| `http_requests_total` | Contador | method, route, status | Pedidos atendidos |
| `http_request_duration_seconds` | Histograma | route | Duração dos pedidos, em baldes de 0,05 s a 10 s |
| `user_events_total` | Contador | event | Ações dos utilizadores |
| `app_errors_total` | Contador | error\_type | Erros por tipo |
| `shoplab_chaos_mode` | Gauge | mode | 1 no modo ativo, 0 nos outros |

Um contador só sobe. O valor útil é a velocidade a que sobe, que o Prometheus calcula com `rate()`.

### Endpoints de saúde

- `/health/live` responde sempre 200 enquanto o processo está vivo. É o healthcheck do Docker.
- `/health` responde 503 se a webapp perdeu a ligação ao ClickHouse.
- `/health/platform` olha para os pedidos dos últimos 60 segundos e responde 503 (`degraded`) se a taxa de erros 5xx passar 10% ou a latência p95 passar 2 000 ms. Sem tráfego, responde 200 com `idle`.

### O modo caos

O modo ativo é um dicionário em memória com cinco parâmetros, que os endpoints consultam a cada pedido. A página `/admin` e o `POST /admin/chaos` só trocam esse dicionário, gravam uma linha WARN em `app_logs` (é essa linha que aparece como anotação vermelha nos gráficos) e atualizam a métrica `shoplab_chaos_mode`.

| Modo | Erros 500 | Latência extra | Pesquisas lentas | Timeouts do gateway | BD em baixo |
| --- | --- | --- | --- | --- | --- |
| normal | 1% | 0 | 4% | 3% | não |
| errors | 25% | 0 | 4% | 3% | não |
| slow | 1% | 600 a 2 500 ms | 50% | 10% | não |
| timeouts | 1% | 0 | 4% | 70% | não |
| attack | 1% | 0 | 4% | 3% | não (os bots são do simulador) |
| outage | — | 0 | — | — | sim: toda a API devolve 503 |

Para acrescentar um modo novo, basta uma entrada no dicionário `CHAOS_MODES` do `app.py`; a página `/admin` mostra-o automaticamente.

## Os scripts de simulação

O tráfego vem de dois sítios: o simulador (`simulator/simulator.py`), que imita pessoas a usar a loja, e os scripts PowerShell da pasta `scripts`, que mudam o cenário e mostram o estado.

### Como o simulador imita pessoas

O simulador lança `USERS` tarefas assíncronas, uma por utilizador virtual. Cada uma repete um ciclo: abre uma sessão, faz um percurso pela loja, espera 1 a 4 segundos e começa outra. Entre cada clique há um "tempo de pensar" de 0,3 a 2,5 segundos, como uma pessoa real a ler a página.

Cada sessão recebe um país (40% Portugal, depois Espanha, Brasil, França, Alemanha, Reino Unido e EUA), um dispositivo (55% mobile, 35% desktop, 10% tablet), um IP e um User-Agent coerentes, e um de quatro perfis:

| Perfil | Peso | Percurso típico |
| --- | --- | --- |
| Navegador | 45% | Vê a página inicial, uma categoria e 1 a 5 produtos; só 30% fazem login. Alguns anónimos tentam adicionar ao carrinho e levam 401. |
| Comprador | 25% | Faz login, navega, adiciona até 3 produtos ao carrinho e faz checkout. Se o checkout der 500 ou 504, metade tenta outra vez. |
| Pesquisador | 20% | Faz login e 2 a 4 pesquisas antes de navegar. |
| Novo cliente | 10% | Regista-se, faz login e navega. |

Há pequenos erros humanos de propósito: 6% dos logins usam a password errada (e tentam de novo) e 4% das visitas pedem um produto que não existe (404).

### As ondas de tráfego

O número de utilizadores ativos não é constante. A função `active_target()` calcula-o com uma onda sinusoidal: vai de 20% a 100% de `USERS` e volta, num ciclo de `WAVE_MINUTES` minutos. Os utilizadores acima desse alvo ficam em pausa. É por isso que o gráfico de sessões ativas no Grafana sobe e desce, como um dia real comprimido.

### Os bots do modo attack

O simulador pergunta à webapp, a cada 5 segundos, qual é o modo caos (`GET /admin/chaos`). Seis tarefas de bot ficam paradas até o modo ser `attack`. Aí começam a fazer várias tentativas de login por segundo com passwords erradas (80% dos pedidos) e a pedir produtos inexistentes (20%), a partir de IPs `185.220.x.x`, de países como RU, CN e KP, e com o User-Agent `python-requests`. É esse padrão que dispara o alerta de força bruta.

### O que o simulador regista

O simulador não escreve no ClickHouse: tudo o que fica guardado é registado pela webapp. Ele só imprime nos seus logs, a cada 30 segundos, um resumo com utilizadores ativos, modo caos, pedidos, erros 5xx, timeouts do lado do cliente (pedidos que demoraram mais de 5 s) e compras concluídas.

### Como ajustar

As variáveis ficam no serviço `simulator` do `docker-compose.yml`. Depois de mudar, aplica com `docker compose up -d simulator`.

| Variável | Por omissão | Efeito |
| --- | --- | --- |
| `USERS` | 30 | Utilizadores virtuais no pico. Mais utilizadores = mais pedidos e mais carga no PC. |
| `WAVE_MINUTES` | 10 | Duração de um ciclo de subida e descida do tráfego |
| `CLIENT_TIMEOUT` | 5 | Segundos que o utilizador espera antes de desistir de um pedido |
| `TARGET` | `http://webapp:8000` | Endereço da plataforma a testar |

Para mudar os perfis, os pesos ou os países, edita as listas no topo do `simulator.py` e a função `run_session`, e reconstrói com `docker compose up -d --build simulator`.

### Os scripts PowerShell

- `scripts\chaos.ps1 -Mode <modo>` envia um `POST /admin/chaos` e mostra o modo ativo. É o equivalente aos cartões da página `/admin`.
- `scripts\status.ps1` mostra o estado dos contentores (`docker compose ps`) e as respostas de `/health/platform`, `/health` e `/admin/chaos`.

Corre ambos com `powershell -ExecutionPolicy Bypass -File .\scripts\<script>.ps1`.

## ClickHouse

O ClickHouse tem dois papéis ao mesmo tempo: guarda os logs da plataforma e é ele próprio monitorizado, através das métricas internas que expõe ao Prometheus.

### Como a base de dados é criada

No primeiro arranque, com o volume de dados vazio, a imagem oficial do ClickHouse executa os ficheiros da pasta `/docker-entrypoint-initdb.d`, onde está montado o `clickhouse/initdb/01_schema.sql`. Esse ficheiro cria:

- a base de dados `platform` e as tabelas `http_requests`, `user_events` e `app_logs`;
- o utilizador `shoplab_app`, que só pode ler e escrever em `platform` (usado pela webapp);
- o utilizador `grafana`, que só pode ler `platform` e as tabelas `system` (usado pelo Grafana e pelo Uptime Kuma).

Separar utilizadores por função é a boa prática: o Grafana não consegue apagar nem alterar dados, mesmo que alguém escreva um `DELETE` num painel. O script só corre uma vez; para o voltar a aplicar depois de o alterares, é preciso apagar o volume com `docker compose down -v`.

### Como as tabelas estão desenhadas

As três tabelas seguem o mesmo padrão:

```sql
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (route, ts)
TTL toDateTime(ts) + INTERVAL 7 DAY
```

- `PARTITION BY` separa os dados por dia, o que permite apagar um dia inteiro de uma vez e ignorar dias fora do período de uma query.
- `ORDER BY` define a ordenação no disco e funciona como índice. Em `http_requests` ordena por rota e depois por hora, por isso filtrar por rota é muito rápido. Em `user_events` ordena por evento, e em `app_logs` por nível.
- `TTL` apaga sozinho as linhas com mais de 7 dias.
- Colunas com poucos valores distintos (rota, país, tipo de erro, nível) usam `LowCardinality(String)`, que as guarda como um dicionário e acelera os `GROUP BY`.

### Como as métricas internas são expostas

O ficheiro `clickhouse/config.d/prometheus.xml` ativa um endpoint nativo na porta 9363. Expõe as tabelas `system.metrics` (valores atuais, como queries em execução e memória), `system.events` (contadores, como queries totais e linhas inseridas) e `system.asynchronous_metrics` (valores calculados periodicamente, como o número de parts). O Prometheus lê-o como lê a webapp.

### Como consultar

A consola web em http://localhost:8123/play aceita SQL diretamente. Três padrões que cobrem a maior parte das perguntas:

Contar por categoria num período:

```sql
SELECT error_type, count() AS n
FROM platform.http_requests
WHERE ts > now() - INTERVAL 15 MINUTE AND error_type != ''
GROUP BY error_type ORDER BY n DESC;
```

Evolução no tempo, em intervalos de 1 minuto:

```sql
SELECT toStartOfMinute(ts) AS minuto,
       countIf(status >= 500) AS erros, count() AS pedidos
FROM platform.http_requests
WHERE ts > now() - INTERVAL 1 HOUR
GROUP BY minuto ORDER BY minuto;
```

Seguir um pedido com erro até à linha de log que o explica, usando o `request_id`:

```sql
SELECT r.ts, r.route, r.status, l.message
FROM platform.http_requests AS r
INNER JOIN platform.app_logs AS l ON l.request_id = toString(r.request_id)
WHERE r.status >= 500
ORDER BY r.ts DESC LIMIT 10;
```

As mesmas queries, com as macros do Grafana no lugar dos períodos fixos, são a base dos painéis (ver a secção do Grafana).

## Prometheus

O Prometheus faz duas coisas, ambas definidas em ficheiros: recolhe métricas (`prometheus/prometheus.yml`) e avalia regras de alerta (`prometheus/alerts.yml`).

### Como recolhe as métricas

O Prometheus funciona em modo pull: é ele que vai buscar as métricas a cada alvo, a um intervalo fixo. Cada alvo é um `job` no `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: shoplab-webapp
    scrape_interval: 10s
    static_configs:
      - targets: ["webapp:8000"]
```

O caminho por omissão é `/metrics`, e o nome `webapp` resolve-se pela rede interna do Docker. A cada recolha, o Prometheus guarda cada série com um carimbo de tempo e acrescenta as etiquetas `job` e `instance`. Cria também a série `up`, com 1 se a recolha correu bem e 0 se falhou, que é a forma mais simples de saber se um serviço está em baixo.

No POC há cinco jobs: `shoplab-webapp` (10 s), `clickhouse` (porta 9363), `uptime-kuma` (20 s, com a API key em `basic_auth`), `prometheus` e `alertmanager`. Para acrescentar um alvo novo, copia um bloco, muda o nome e o endereço, e recarrega a configuração (ver "Aplicar alterações").

### Como é feita uma regra de alerta

Uma regra é uma query PromQL com um limite. Esta é a da taxa de erros:

```yaml
- alert: PlataformaTaxaErrosAlta
  expr: |
    sum(rate(http_requests_total{status=~"5.."}[2m]))
      / sum(rate(http_requests_total[2m])) > 0.05
  for: 1m
  labels:
    severity: critical
  annotations:
    summary: "Taxa de erros 5xx da ShopLab em {{ $value | humanizePercentage }} (limite 5%)"
```

- `expr` é a condição. `rate(...[2m])` calcula quantos pedidos por segundo houve nos últimos 2 minutos; a divisão dá a fração de erros 5xx; `> 0.05` é o limite. Se a expressão devolver algum valor, a condição é verdadeira.
- `for` é o tempo que a condição tem de se manter antes de o alerta disparar. Evita alertas por picos de segundos.
- `labels` são etiquetas que viajam com o alerta e que o Alertmanager usa para decidir para onde o mandar (por exemplo, `critical` para o piquete, `warning` para um canal de equipa).
- `annotations` são o texto legível. `{{ $value }}` é o valor atual da expressão e `{{ $labels.x }}` uma etiqueta da série.

O Prometheus avalia as regras a cada 15 segundos (`evaluation_interval`). Cada alerta passa por três estados: `inactive` (condição falsa), `pending` (verdadeira, mas ainda dentro do `for`) e `firing` (verdadeira há tempo suficiente). Só no estado `firing` é enviado ao Alertmanager.

### Os quatro padrões usados no POC

| Padrão | Exemplo no POC | Expressão |
| --- | --- | --- |
| Serviço em baixo | `ClickHouseDown` | `up{job="clickhouse"} == 0` |
| Proporção acima de um limite | `PlataformaTaxaErrosAlta` | erros/s a dividir por pedidos/s `> 0.05` |
| Latência acima de um limite | `PlataformaLenta` | `histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket[2m]))) > 1` |
| Ritmo de um evento | `PlataformaForcaBrutaLogin` | `sum(rate(http_requests_total{route="/api/login",status="401"}[2m])) * 60 > 60` |

O `histogram_quantile` estima o percentil a partir dos baldes do histograma: o p95 é a duração abaixo da qual ficam 95% dos pedidos. A multiplicação por 60 converte "por segundo" em "por minuto", que é mais fácil de ler num limite.

### Como criar uma regra nova

1. Escreve e testa a expressão primeiro em http://localhost:9090 → Graph. Se o gráfico mostra o que esperas e consegues ver onde passaria o limite, a expressão está certa.
2. Acrescenta a regra ao `prometheus/alerts.yml`, dentro de um dos grupos, com `alert`, `expr`, `for`, `labels` e `annotations`.
3. Valida a sintaxe sem parar nada:

```powershell
docker exec shoplab-prometheus promtool check rules /etc/prometheus/alerts.yml
```

4. Aplica (ver a seguir) e confirma em Alerts que a regra nova aparece como `inactive`.

### Aplicar alterações

O Prometheus foi arrancado com `--web.enable-lifecycle`, por isso relê os dois ficheiros sem reiniciar:

```powershell
curl.exe -X POST http://localhost:9090/-/reload
```

Se a configuração tiver erros, o reload é recusado e o Prometheus continua com a anterior; o motivo aparece em `docker compose logs prometheus`.

## Alertmanager

O Prometheus decide quando algo está mal; o Alertmanager decide quem é avisado, como e com que frequência. Toda a configuração está em `alertmanager/alertmanager.yml`:

```yaml
route:
  receiver: logger
  group_by: [alertname]
  group_wait: 10s
  group_interval: 1m
  repeat_interval: 1h

receivers:
  - name: logger
    webhook_configs:
      - url: http://alert-logger:5001/
        send_resolved: true
```

### O que cada opção faz

| Opção | Valor no POC | Efeito |
| --- | --- | --- |
| `receiver` | logger | Destino por omissão de todos os alertas |
| `group_by` | alertname | Alertas com o mesmo nome seguem juntos numa só notificação (por exemplo, vários monitores do Uptime Kuma em baixo ao mesmo tempo) |
| `group_wait` | 10 s | Espera antes da primeira notificação de um grupo, para juntar alertas que chegam quase em simultâneo |
| `group_interval` | 1 min | Espera antes de notificar alterações num grupo já enviado |
| `repeat_interval` | 1 h | De quanto em quanto tempo volta a lembrar um alerta que continua ativo |
| `send_resolved` | true | Envia também o aviso de que o problema passou (`RESOLVED`) |

### Encaminhar por gravidade

Num ambiente real, é comum mandar os alertas `critical` para um canal de piquete e os `warning` para um canal de equipa. Faz-se com sub-rotas que olham para as etiquetas definidas nas regras do Prometheus:

```yaml
route:
  receiver: equipa
  group_by: [alertname]
  routes:
    - matchers: ['severity="critical"']
      receiver: piquete

receivers:
  - name: equipa
    msteams_configs:
      - webhook_url: https://webhook-do-canal-da-equipa
  - name: piquete
    msteams_configs:
      - webhook_url: https://webhook-do-canal-de-piquete
```

O Alertmanager suporta diretamente email, Slack, Microsoft Teams, Telegram, Discord, PagerDuty, Opsgenie e webhooks genéricos. Depois de mudar o ficheiro, aplica com `docker compose restart alertmanager`.

### Silenciar um alerta

Durante uma manutenção planeada, não interessa ser avisado. Em http://localhost:9093 → New Silence escolhes as etiquetas a silenciar (por exemplo `alertname="ClickHouseDown"`), a duração e um comentário. O alerta continua a ser avaliado no Prometheus, mas o Alertmanager não o entrega até o silêncio expirar.

### O alert-logger

O destino `logger` é o alert-logger, um recetor de webhooks em Python que imprime cada alerta como `[FIRING]` ou `[RESOLVED]`, seguido do nome, da gravidade e do resumo. Substitui o canal real para que se veja o percurso completo de um alerta sem configurar contas externas. Os alertas aparecem em `docker compose logs -f alert-logger`.

## Grafana: datasources e dashboards

O Grafana não guarda dados: liga-se a fontes (datasources) e desenha o resultado de queries. No POC tudo é carregado de ficheiros no arranque, mas cada passo pode ser feito à mão na interface.

### Como as datasources são ligadas

O ficheiro `grafana/provisioning/datasources/datasources.yml` cria três datasources no arranque: ClickHouse (com o plugin `grafana-clickhouse-datasource`, instalado pela variável `GF_INSTALL_PLUGINS` do compose), Prometheus e Alertmanager. Para criar uma à mão:

1. Vai a **Connections → Data sources → Add new data source**.
2. Escolhe o tipo (por exemplo **ClickHouse**).
3. Preenche a ligação. Para o ClickHouse: Server address `clickhouse`, Server port `8123`, Protocol **HTTP**, Username `grafana`, Password `grafana123`, Default database `platform`. Para o Prometheus basta o URL `http://prometheus:9090`.
4. Carrega em **Save & test**. Deve aparecer uma mensagem verde de ligação bem-sucedida.

Os nomes `clickhouse` e `prometheus` funcionam porque o Grafana está na mesma rede Docker. A partir do teu PC, os mesmos serviços estariam em `localhost`.

### Como criar um dashboard

Como exemplo, um painel com os pedidos por minuto de cada classe de resposta, lidos do ClickHouse.

1. Vai a **Dashboards → New → New dashboard → Add visualization** e escolhe a datasource **ClickHouse**.
2. No editor de query, muda para **SQL Editor** e escolhe Query Type **Time Series**.
3. Escreve a query:

```sql
SELECT $__timeInterval(ts) AS time,
       countIf(status < 400) AS ok,
       countIf(status >= 400 AND status < 500) AS `4xx`,
       countIf(status >= 500) AS `5xx`
FROM platform.http_requests
WHERE $__timeFilter(ts)
GROUP BY time ORDER BY time
```

4. Carrega em **Run query**. O gráfico aparece em cima, com uma linha por coluna.
5. No painel da direita, escolhe a visualização (**Time series**), dá um título e, em **Standard options → Unit**, escolhe a unidade. Para dar uma cor fixa a uma série (vermelho para `5xx`), usa **Overrides → Add field override → Fields with name**.
6. Carrega em **Back to dashboard**, e depois no ícone de gravar: dá um nome e uma pasta.
7. No topo, escolhe o período (por exemplo, últimos 30 minutos) e a atualização automática (10 s).

As macros são o que liga a query ao seletor de tempo do dashboard:

| Macro | Substituída por |
| --- | --- |
| `$__timeFilter(ts)` | Um filtro com o início e o fim do período escolhido |
| `$__timeInterval(ts)` | O início do intervalo de cada ponto do gráfico, ajustado ao período (mais largo em períodos longos) |
| `$__interval_s` | A largura desse intervalo, em segundos; serve para converter contagens em "por segundo" |

Sem `$__timeFilter`, a query lê a tabela inteira e ignora o seletor de tempo.

### As regras de uma query para cada visualização

| Visualização | Query Type | O que a query deve devolver |
| --- | --- | --- |
| Time series | Time Series | Uma coluna `time` e uma coluna numérica por linha do gráfico |
| Stat | Table | Uma linha com um número (por exemplo `SELECT count() ...`) |
| Bar gauge | Table | Uma linha com uma coluna numérica por barra (é assim o funil do POC) |
| Table | Table | Qualquer resultado; cada coluna vira uma coluna da tabela |
| Pie chart | Table | Uma linha com uma coluna numérica por fatia |

No ClickHouse, um nome de coluna com acentos ou espaços tem de ir entre acentos graves (`` `Sessões` ``), caso contrário a query dá erro de sintaxe.

### Um painel a partir do Prometheus

O processo é igual, mas com a datasource **Prometheus**. No editor, muda para **Code** e escreve PromQL; por exemplo, para pedidos por segundo:

```promql
sum(rate(http_requests_total[1m]))
```

Escolhe a visualização **Stat** e a unidade **requests/sec**. Em **Legend**, `{{route}}` dá o nome de cada série a partir de uma etiqueta, se a query devolver várias (por exemplo com `sum by (route)`).

### Filtros no topo do dashboard (variáveis)

Para escolher a rota numa lista e todos os painéis filtrarem por ela:

1. No dashboard, vai a **Settings → Variables → Add variable**.
2. Tipo **Query**, nome `route`, datasource ClickHouse, query `SELECT DISTINCT route FROM platform.http_requests`.
3. Ativa **Multi-value** e **Include All option**, e grava.
4. Nas queries dos painéis, acrescenta ao `WHERE`: `AND route IN (${route:singlequote})`.

### Guardar dashboards em ficheiro

Os dashboards criados na interface ficam na base de dados interna do Grafana (o volume `grafana-data`) e perdem-se com `docker compose down -v`. Os do POC são ficheiros JSON em `grafana/dashboards`, carregados pelo `grafana/provisioning/dashboards/dashboards.yml`. Por isso, a interface não deixa gravar alterações por cima deles: usa **Save as** para fazer uma cópia editável.

Para transformar um dashboard teu em ficheiro, usa **Export → Export as JSON**, grava o JSON na pasta `grafana/dashboards` e espera uns segundos: o Grafana procura ficheiros novos nessa pasta periodicamente e carrega-o sozinho.

## Grafana: como criar um alerta

O Grafana tem o seu próprio motor de alertas, independente do Prometheus. A grande vantagem é poder alertar sobre qualquer datasource, incluindo os logs no ClickHouse, e configurar tudo na interface. No POC os alertas vivem no Prometheus, mas aparecem também em **Alerting → Alert rules**, marcados como geridos pela datasource.

Um alerta do Grafana precisa de três peças: um **contact point** (para onde vai), a **regra** (quando dispara) e, opcionalmente, uma **notification policy** (como escolher o contact point pelas etiquetas).

### 1. Criar o contact point

1. Vai a **Alerting → Contact points → Add contact point**.
2. Dá-lhe um nome, por exemplo `alert-logger`.
3. Em **Integration** escolhe **Webhook** e, no URL, `http://alert-logger:5001/`.
4. Carrega em **Test** para enviar um alerta de teste e grava.

O formato que o Grafana envia por webhook é compatível com o do Alertmanager, por isso o alert-logger imprime estes alertas tal como imprime os do Prometheus. Num ambiente real, escolherias aqui Email, Microsoft Teams, Slack ou Telegram.

### 2. Criar a regra

Como exemplo, um alerta de negócio que só os logs permitem: nenhuma compra nos últimos 10 minutos, mesmo com a loja de pé.

1. Vai a **Alerting → Alert rules → New alert rule**.
2. **Nome:** `Sem compras em 10 minutos`.
3. **Query e condição:** escolhe a datasource **ClickHouse**, muda para **SQL Editor**, Query Type **Table**, e escreve:

```sql
SELECT countIf(event = 'purchase') AS compras
FROM platform.user_events
WHERE ts > now() - INTERVAL 10 MINUTE
```

Na condição, escolhe **IS BELOW** `1`. Carrega em **Preview** para ver o valor atual e se a condição seria verdadeira agora.

4. **Pasta e etiquetas:** cria ou escolhe a pasta `ShopLab` e acrescenta a etiqueta `severity` = `warning`.
5. **Avaliação:** cria um evaluation group, por exemplo `negocio`, com intervalo de **1m**, e define o **Pending period** em **2m**. O grupo diz de quanto em quanto tempo a query corre; o pending period é o equivalente ao `for` do Prometheus.
6. **Notificações:** escolhe o contact point `alert-logger`.
7. **Mensagem:** em **Summary** escreve `Nenhuma compra nos últimos 10 minutos`. Pode usar o valor da query, por exemplo `{{ $values.A }}`.
8. Carrega em **Save rule and exit**.

Para testar, ativa o modo `outage` ou `timeouts`: as compras param e, ao fim de uns 3 minutos, o alerta chega ao alert-logger.

### Regras com séries temporais

Se a query devolver uma série (por exemplo PromQL, ou SQL em modo Time Series), é preciso reduzi-la a um número antes de comparar. Nas opções avançadas da condição, a expressão **Reduce** escolhe que valor usar (**Last** para o mais recente, **Mean** para a média do período) e a expressão **Threshold** faz a comparação. Uma query SQL que devolve uma linha com um número, como a de cima, não precisa disto.

### Sem dados e erros

Em **Configure no data and error handling** decides o que acontece se a query não devolver nada ou falhar. O valor por omissão (**No Data**) gera uma notificação própria. Para um alerta como "sem compras", é mais útil escolher **Alerting**: se o ClickHouse não responder, também queres saber.

### Encaminhar com notification policies

Em vez de escolher o contact point em cada regra, podes fazê-lo por etiquetas, como no Alertmanager. Em **Alerting → Notification policies**, a política por omissão define o contact point geral; com **New child policy** e o matcher `severity = critical` mandas os críticos para outro contact point. As opções de agrupamento e repetição são as mesmas do Alertmanager.

### Onde criar cada alerta

| Onde | Melhor para | Configuração |
| --- | --- | --- |
| Regras do Prometheus | Métricas: taxa de erros, latência, serviços em baixo | Ficheiro `alerts.yml`, versionado com o projeto |
| Alertas do Grafana | Logs e dados de negócio no ClickHouse, ou combinar datasources | Interface do Grafana |
| Notificações do Uptime Kuma | Disponibilidade simples, por um caminho independente do Prometheus | Interface do Uptime Kuma |

A regra prática é não criar a mesma condição em dois sítios: o resultado são notificações duplicadas e dúvidas sobre qual é a fonte de verdade.

## Uptime Kuma

O Uptime Kuma testa os serviços de fora, como um utilizador faria, e não depende do Prometheus nem do Grafana. No POC, o contentor `kuma-setup` configura-o automaticamente através da API (`kuma-setup/setup_kuma.py`); esta secção mostra como fazer o mesmo à mão na interface, em http://localhost:3001.

### Os monitores do POC

| Monitor | Tipo | Alvo | Fica DOWN quando |
| --- | --- | --- | --- |
| Contentor webapp, simulator, clickhouse, prometheus, alertmanager, alert-logger, grafana | Docker Container | Nome do contentor (`shoplab-...`) | O contentor para, ou o seu healthcheck falha |
| Loja (página inicial) | HTTP(s) | `http://webapp:8000/` | A página não responde ou devolve erro |
| API e ligação ao ClickHouse | HTTP(s) | `http://webapp:8000/health` | A webapp perde a ligação ao ClickHouse (503) |
| Saúde da plataforma | HTTP(s) | `http://webapp:8000/health/platform` | Erros 5xx acima de 10% ou p95 acima de 2 s no último minuto (503) |
| Logs a chegar ao ClickHouse | HTTP(s) - Keyword | Query SQL ao ClickHouse por HTTP | A resposta não contém `FRESH`, ou seja, não entraram pedidos no último minuto |
| ClickHouse, Prometheus, Alertmanager, Grafana | HTTP(s) | `/ping`, `/-/healthy`, `/api/health` de cada um | O serviço não responde |

Todos usam um intervalo de 20 segundos e uma nova tentativa antes de passarem a DOWN.

### Criar um monitor HTTP

1. Carrega em **Add New Monitor**.
2. **Monitor Type:** HTTP(s).
3. **Friendly Name:** `Saúde da plataforma`.
4. **URL:** `http://webapp:8000/health/platform`. Como o Uptime Kuma está na rede Docker, usa o nome do serviço, não `localhost`.
5. **Heartbeat Interval:** `20` segundos.
6. **Retries:** `1`, e **Heartbeat Retry Interval:** `20`. Com isto, uma falha põe o monitor em PENDING e só a segunda seguida o põe em DOWN, o que evita alarmes por uma falha isolada.
7. Em **Advanced**, **Accepted Status Codes** fica em `200-299`. É por isso que um 503 do `/health/platform` conta como falha.
8. Carrega em **Save**.

### Criar um monitor de keyword

Serve para verificar o conteúdo da resposta e não só se há resposta. O monitor "Logs a chegar ao ClickHouse" usa a interface HTTP do ClickHouse para correr uma query:

1. **Monitor Type:** HTTP(s) - Keyword.
2. **URL:** `http://clickhouse:8123/?user=grafana&password=grafana123&query=` seguido da query codificada para URL: `SELECT if(count() > 0, 'FRESH', 'STALE') FROM platform.http_requests WHERE ts > now() - INTERVAL 1 MINUTE`. O URL completo está no `setup_kuma.py`.
3. **Keyword:** `FRESH`.
4. Intervalo e tentativas como acima, e **Save**.

O mesmo truque serve para qualquer verificação de negócio que se escreva em SQL e devolva uma palavra.

### Monitorizar contentores Docker

Primeiro liga o Uptime Kuma ao Docker (uma vez só):

1. Vai a **Settings → Docker Hosts → Setup Docker Host**.
2. **Friendly Name:** `Docker local`, **Connection Type:** Socket, **Docker Daemon:** `/var/run/docker.sock`.
3. Carrega em **Test** e depois em **Save**.

Isto só funciona porque o `docker-compose.yml` monta o socket do Docker no contentor do Uptime Kuma (`/var/run/docker.sock:/var/run/docker.sock`). Depois, para cada contentor:

1. **Add New Monitor → Monitor Type:** Docker Container.
2. **Container Name / ID:** por exemplo `shoplab-webapp` (a lista de nomes sai de `docker ps --format "{{.Names}}"`).
3. **Docker Host:** `Docker local`, e **Save**.

### Criar a página de estado

1. Vai a **Status Pages → New Status Page**, com nome `ShopLab · Estado da plataforma` e slug `shoplab`.
2. No editor, escreve uma descrição e carrega em **Add Group** para cada grupo ("Plataforma ShopLab", "Contentores", "Observabilidade").
3. Em cada grupo, usa **Add a monitor** para escolher os monitores.
4. Carrega em **Save**. A página fica pública em http://localhost:3001/status/shoplab.

### Configurar notificações

No POC, o Uptime Kuma não notifica diretamente: o Prometheus lê o estado dos monitores e os alertas seguem pelo Alertmanager. Para o Uptime Kuma avisar por si mesmo (útil para ter um caminho de alerta que funciona mesmo com o Prometheus em baixo):

1. Vai a **Settings → Notifications → Setup Notification**.
2. Escolhe o **Notification Type** (Telegram, Discord, Microsoft Teams, Slack, Email SMTP, entre outros) e preenche os dados desse canal.
3. Ativa **Default enabled** para ser usada nos monitores novos e, se quiseres, **Apply on all existing monitors**.
4. Carrega em **Test** e depois em **Save**.

Cada monitor tem, no seu formulário, uma caixa por notificação configurada, para escolher quais recebem alertas. Não vale a pena apontar uma notificação Webhook ao alert-logger: o Uptime Kuma usa outro formato de mensagem, e o alert-logger não a mostraria.

### Janelas de manutenção

Em **Maintenance → Schedule Maintenance** escolhes os monitores afetados e o período (manual, único ou recorrente). Durante a janela, esses monitores ficam em estado de manutenção (azul) e não geram notificações; no Prometheus aparecem com o valor 3, que o alerta `MonitorEmBaixo` ignora.

### Expor os dados ao Prometheus

O Uptime Kuma publica o estado de todos os monitores em `/metrics`, protegido por API key:

1. Vai a **Settings → API Keys → Add API Key**, dá um nome (`prometheus`) e sem data de expiração.
2. Copia a chave (só é mostrada uma vez).
3. No Prometheus, a chave vai como password de `basic_auth`, com o utilizador vazio. No POC, o kuma-setup grava-a num volume partilhado e o `prometheus.yml` lê-a com `password_file`.

As métricas expostas são `monitor_status` (1 UP, 0 DOWN, 2 PENDING, 3 manutenção), `monitor_response_time` (ms) e, para sites com HTTPS, `monitor_cert_days_remaining` e `monitor_cert_is_valid`. Todas têm as etiquetas `monitor_name`, `monitor_type` e `monitor_url`, que é o que permite ao alerta dizer qual monitor caiu.

## Percurso de um incidente

Ao ativar o modo `errors`, o primeiro sinal aparece no Grafana em menos de 10 segundos e o primeiro alerta chega ao alert-logger por volta de 1,5 minutos. Os tempos abaixo são estimativas calculadas a partir dos intervalos configurados no POC; na prática variam uns segundos de cada vez.

| Tempo (aprox.) | Onde | O que acontece |
| --- | --- | --- |
| 0 s | Webapp | Escolhes `errors` em /admin. O modo caos muda, fica uma linha WARN em `app_logs` e a métrica `shoplab_chaos_mode` passa a marcar `errors`. |
| 1 s | Webapp → ClickHouse | Os primeiros erros 500 são gravados no lote seguinte em `http_requests` e `app_logs`. |
| até 10 s | Grafana | O dashboard Atividade atualiza: a faixa vermelha 5xx sobe, aparece a anotação vermelha da mudança de modo e a tabela de logs enche-se de deadlocks e exceções. |
| até 10 s | Prometheus | A recolha seguinte ao `/metrics` da webapp traz os contadores de erros a subir. |
| cerca de 20 a 35 s | Prometheus | A taxa de erros dos últimos 2 minutos passa 5%: o alerta `PlataformaTaxaErrosAlta` fica `pending`. |
| cerca de 25 a 45 s | Uptime Kuma | A taxa de erros do último minuto passa 10%; a verificação seguinte ao `/health/platform` recebe 503 e o monitor fica PENDING. |
| cerca de 45 a 65 s | Uptime Kuma | A nova tentativa falha também: "Saúde da plataforma" fica DOWN, a vermelho, também na página de estado. |
| cerca de 1 min 20 s a 1 min 35 s | Prometheus → Alertmanager | Passou o `for: 1m`: o alerta fica `firing` e é enviado ao Alertmanager. |
| cerca de 1 min 30 s a 1 min 45 s | alert-logger | Depois do `group_wait` de 10 s chega `[FIRING] PlataformaTaxaErrosAlta`. |
| cerca de 1 min 45 s a 2 min 15 s | Prometheus → alert-logger | O Prometheus lê o `monitor_status = 0` do Uptime Kuma, passa o `for: 30s` e chega `[FIRING] MonitorEmBaixo` para "Saúde da plataforma". |

No regresso ao modo `normal`, o caminho faz-se ao contrário, mas mais devagar, porque as janelas de tempo têm de "esquecer" os erros:

- O Uptime Kuma volta a verde em cerca de 1 minuto, quando a janela de 60 segundos do `/health/platform` desce abaixo de 10%.
- A taxa de erros dos últimos 2 minutos desce abaixo de 5% ao fim de cerca de 1 min 40 s. O alerta resolve-se logo (o `for` só se aplica a disparar), e o Alertmanager envia o `[RESOLVED]` na próxima janela de `group_interval`, até 1 minuto depois.

Este percurso mostra porque é que cada camada existe: o Grafana dá o detalhe mais cedo, para quem está a olhar; o Uptime Kuma confirma o impacto como o utilizador o sente; e o Prometheus com o Alertmanager garantem que alguém é avisado mesmo que ninguém esteja a olhar, sem disparar por um pico de segundos.

Para afinar a rapidez de cada alerta, os botões são sempre os mesmos: a janela da query (`[2m]` no PromQL, 60 s no `/health/platform`), o tempo mínimo (`for` no Prometheus, Retries no Uptime Kuma, Pending period no Grafana) e os tempos do Alertmanager (`group_wait`, `group_interval`). Janelas e tempos mais curtos avisam mais cedo, mas também disparam mais vezes por picos passageiros.
