# POC ShopLab: observabilidade com ClickHouse, Prometheus, Grafana e Uptime Kuma no Windows 11

Oct 21, 2026 · @Bruno

## Visão geral

O POC põe a correr no teu PC uma loja online fictícia, a ShopLab, com utilizadores simulados a usá-la, e toda a stack de observabilidade à volta dela. Tudo corre em 9 contentores Docker, arrancados com um único comando (um deles só corre uma vez, para configurar o Uptime Kuma).

A ideia central é haver três perspetivas sobre a mesma plataforma:

- **O que os utilizadores fazem e que erros acontecem.** A plataforma web escreve cada pedido HTTP, cada ação de utilizador (login, pesquisa, carrinho, compra) e cada erro em tabelas do ClickHouse. O Grafana lê estas tabelas diretamente e mostra-as no dashboard "Atividade da Plataforma".
- **Como estão os serviços por dentro.** A plataforma e o ClickHouse expõem métricas em `/metrics`. O Prometheus recolhe-as, guarda-as e avalia regras de alerta (taxa de erros, latência, timeouts, ClickHouse em baixo). Os alertas seguem para o Alertmanager, que os entrega ao alert-logger.
- **Se tudo está de pé, visto de fora.** O Uptime Kuma verifica cada contentor e testa a plataforma como um utilizador faria, incluindo um teste de saúde que fica vermelho quando a taxa de erros ou a latência passam de um limite. Publica também uma página de estado.

O caminho dos dados é este:

1. O **simulator** cria utilizadores virtuais que navegam, pesquisam, fazem login, adicionam ao carrinho e compram.
2. A **webapp** (ShopLab, em Python/FastAPI) responde-lhes e, de vez em quando, falha de propósito: erros 500, timeouts do gateway de pagamento, logins falhados, produtos inexistentes.
3. Cada pedido, ação e erro é gravado no **ClickHouse**, nas tabelas `http_requests`, `user_events` e `app_logs`.
4. O **Prometheus** recolhe métricas da webapp, do ClickHouse e do Uptime Kuma, e dispara alertas via **Alertmanager** para o **alert-logger**.
5. O **Grafana** mostra dois dashboards: atividade da plataforma (dados do ClickHouse) e infraestrutura (dados do Prometheus).
6. O **Uptime Kuma** vigia contentores e endpoints, e é configurado automaticamente pelo contentor **kuma-setup**.

&#91;embedded content: arquitetura do POC · 10 componentes e o caminho dos dados\]

A webapp é o centro: grava cada pedido, ação e erro no ClickHouse (que o Grafana lê em SQL), expõe métricas ao Prometheus (que dispara alertas pelo Alertmanager) e é testada de fora pelo Uptime Kuma, que também vigia os contentores pelo socket do Docker. O kuma-setup não aparece no diagrama porque só corre uma vez, no arranque, para configurar o Uptime Kuma.

Cada componente tem um único papel: dois geram a atividade, um guarda-a, três observam-na e dois entregam o resultado a quem tem de agir.

| Componente | O que é | O que faz neste POC | Papel na monitorização |
| --- | --- | --- | --- |
| Simulador | Script Python com utilizadores virtuais | Navega, pesquisa, faz login, adiciona ao carrinho e compra, com o tráfego a subir e descer em ondas de 10 minutos | Gera a carga: sem ele, não haveria nada para observar |
| Webapp (ShopLab) | Loja online em Python/FastAPI | Responde aos utilizadores, falha de propósito (erros, timeouts, logins falhados) e tem o modo caos em /admin | É o sistema monitorizado: grava os seus logs no ClickHouse, expõe métricas em /metrics e saúde em /health |
| ClickHouse | Base de dados colunar para analytics | Guarda cada pedido HTTP, ação de utilizador e linha de log nas tabelas http\_requests, user\_events e app\_logs | Armazém dos logs e eventos: responde a "o que fizeram os utilizadores e que erros houve", com detalhe até ao pedido individual |
| Prometheus | Base de dados de séries temporais | Recolhe métricas da webapp, do ClickHouse e do Uptime Kuma a cada 10 a 20 segundos e avalia 11 regras de alerta | Motor de métricas e alertas: decide quando algo está mal (taxa de erros, latência, timeouts, serviços em baixo) |
| Alertmanager | Gestor de alertas do Prometheus | Recebe os alertas, agrupa-os, evita repetições e envia-os ao alert-logger | Encaminhamento: decide quem é avisado, quando e com que frequência |
| alert-logger | Recetor de webhooks em Python | Imprime cada alerta como uma linha FIRING ou RESOLVED nos logs do contentor | Notificação: substitui o email, Slack ou Teams de um ambiente real |
| Grafana | Plataforma de dashboards | Mostra o dashboard Atividade da Plataforma (SQL ao ClickHouse) e o Infraestrutura (PromQL ao Prometheus) | Visualização: junta logs e métricas num só sítio para perceber o que se passa e investigar incidentes |
| Uptime Kuma | Ferramenta de monitorização de disponibilidade | Verifica a cada 20 segundos os 7 contentores e 8 endpoints, e publica a página de estado | Visão de fora (black-box): confirma se cada serviço está de pé e a responder, como um utilizador o veria |
| kuma-setup | Script Python que corre uma vez | Cria a conta, os monitores, a página de estado e a API key do Uptime Kuma | Automatização da configuração: não monitoriza nada, prepara o Uptime Kuma e termina |

A webapp tem um **modo caos**, controlado numa página web, que permite provocar incidentes (muitos erros, lentidão, timeouts, base de dados em baixo) e ver cada camada da stack a reagir.

## Pré-requisitos no Windows 11

Só precisas do Docker Desktop com o motor WSL 2; tudo o resto (Python, ClickHouse, Grafana, etc.) corre dentro de contentores.

| Requisito | Mínimo | Recomendado |
| --- | --- | --- |
| Windows | Windows 11 64 bits, virtualização ativa na BIOS | Windows 11 23H2 ou mais recente |
| RAM do PC | 8 GB | 16 GB |
| RAM dada ao Docker | 4 GB | 6 GB |
| CPU | 4 núcleos | 6+ núcleos |
| Disco livre | 10 GB | 20 GB |
| Internet | Necessária no primeiro arranque (imagens, pacotes Python, plugin do Grafana) | — |

Estas portas têm de estar livres no PC: **3000** (Grafana), **3001** (Uptime Kuma), **8080** (loja), **8123** e **9363** (ClickHouse), **9090** (Prometheus) e **9093** (Alertmanager).

Para confirmar que nenhuma está ocupada, corre no PowerShell:

```powershell
Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 3000,3001,8080,8123,9090,9093,9363
```

Se não aparecer nada, estão todas livres. Se alguma estiver ocupada, muda o número da esquerda no mapeamento de portas do `docker-compose.yml` (por exemplo `"3002:3000"`).

Se tinhas o POC anterior (`clickhouse-poc`) a correr neste PC, para-o primeiro com `docker compose down` na pasta dele, para libertar as portas.

## Passo a passo

São 8 passos e cerca de 20 minutos na primeira vez, a maior parte à espera de downloads. Usa sempre o **PowerShell** (não o Git Bash, que estraga o caminho do socket do Docker).

### Passo 1: ativar o WSL 2

Abre o PowerShell **como administrador** (botão direito no menu Iniciar → Terminal (Admin)) e corre:

```powershell
wsl --install
```

Reinicia o PC quando pedir. Se o WSL já estiver instalado, o comando só diz isso e podes seguir.

**O que acontece:** o Windows instala o WSL 2, uma camada que corre um kernel Linux real dentro do Windows. Os contentores desta stack são Linux, e é aqui que vão correr.

### Passo 2: instalar e configurar o Docker Desktop

1. Descarrega o Docker Desktop em docker.com/products/docker-desktop e instala com as opções por omissão (deixa marcado "Use WSL 2 instead of Hyper-V").
2. Abre o Docker Desktop e espera que o ícone da baleia na barra de tarefas fique estável ("Engine running").
3. Em **Settings → General**, confirma que "Use the WSL 2 based engine" está ligado.
4. Num PowerShell normal, confirma:

```powershell
docker --version
docker compose version
docker run --rm hello-world
```

O último comando deve mostrar "Hello from Docker!".

**O que acontece:** o Docker Desktop instala o motor Docker dentro do WSL 2 e liga-o ao Windows. A partir daqui, os comandos `docker` no PowerShell controlam contentores Linux, e as portas que eles publicam ficam acessíveis em `localhost` no teu browser.

Se a tua máquina tiver pouca RAM, podes limitar o que o WSL usa: cria o ficheiro `C:\Users\<o-teu-utilizador>\.wslconfig` com as linhas `[wsl2]` e `memory=6GB`, e corre `wsl --shutdown`.

### Passo 3: extrair o projeto

Extrai o `shoplab-poc.zip` para a tua pasta pessoal, de forma a ficares com `C:\Users\<o-teu-utilizador>\shoplab-poc`. Depois:

```powershell
cd $HOME\shoplab-poc
dir
```

Deves ver o `docker-compose.yml`, o `README.md` e as pastas `clickhouse`, `webapp`, `simulator`, `kuma-setup`, `prometheus`, `alertmanager`, `alert-logger`, `grafana` e `scripts`. Todos os comandos seguintes são corridos dentro desta pasta.

**O que acontece:** cada pasta tem a configuração ou o código de um serviço. O `docker-compose.yml` é o ficheiro que descreve os 9 contentores, como se ligam e que portas expõem.

### Passo 4: construir e arrancar a stack

```powershell
docker compose up -d --build
```

Na primeira vez demora 5 a 10 minutos. Se o Windows Firewall perguntar se permite o acesso ao Docker, aceita para redes privadas.

**O que acontece:** o Docker descarrega as imagens oficiais (ClickHouse, Prometheus, Alertmanager, Grafana, Uptime Kuma, Python) e constrói as três imagens do projeto (webapp, simulator e kuma-setup). Depois arranca os contentores pela ordem certa: o ClickHouse primeiro; a webapp só quando o ClickHouse responde; o simulador só quando a webapp responde.

### Passo 5: confirmar que os contentores estão de pé

```powershell
docker compose ps -a
```

Oito serviços devem estar `running` e o `shoplab-clickhouse` e o `shoplab-webapp` devem mostrar `(healthy)`. O `shoplab-kuma-setup` deve aparecer como `exited (0)`: é normal, ele configura o Uptime Kuma e termina.

Confirma que a configuração do Uptime Kuma correu bem:

```powershell
docker compose logs kuma-setup
```

A última linha deve ser "Configuração do Uptime Kuma concluída." Se aparecer `exited (1)` ou erros, corre `docker compose up -d kuma-setup` para tentar outra vez (às vezes o Uptime Kuma ainda não estava pronto).

**O que acontece:** no primeiro arranque, o ClickHouse executa o `clickhouse/initdb/01_schema.sql`, que cria a base de dados `platform`, as três tabelas e os utilizadores `shoplab_app` (escrita) e `grafana` (só leitura). O kuma-setup cria a conta de administrador do Uptime Kuma, liga-o ao Docker, cria 15 monitores, a página de estado e uma API key que o Prometheus usa para ler as métricas do Uptime Kuma.

### Passo 6: confirmar que o tráfego está a gerar dados

```powershell
docker compose logs simulator
```

Deves ver "plataforma disponível" e, a cada 30 segundos, uma linha com o número de pedidos, erros e compras. Depois confirma que os dados chegam ao ClickHouse:

```powershell
docker exec shoplab-clickhouse clickhouse-client --user admin --password admin123 -q "SELECT count() FROM platform.http_requests"
```

Corre duas vezes com uns segundos de intervalo: o número deve subir.

**O que acontece:** o simulador lança até 30 utilizadores virtuais, com perfis diferentes (quem só navega, quem compra, quem pesquisa, quem se regista). O número de utilizadores ativos sobe e desce em ciclos de 10 minutos, como um dia real comprimido. A webapp responde e grava em lotes, a cada segundo, cada pedido em `http_requests`, cada ação em `user_events` e cada aviso ou erro em `app_logs`.

### Passo 7: abrir as interfaces

No browser, abre cada um destes endereços:

- **Loja:** http://localhost:8080
- **Grafana:** http://localhost:3000 (admin / admin123). Abre diretamente no dashboard "ShopLab · Atividade da Plataforma".
- **Uptime Kuma:** http://localhost:3001 (admin / ShopLab2026!)
- **Prometheus:** http://localhost:9090, em Status → Targets os 5 jobs devem estar UP

**O que acontece:** o Grafana instalou no arranque o plugin do ClickHouse e carregou automaticamente as datasources (ClickHouse, Prometheus, Alertmanager) e os dois dashboards a partir da pasta `grafana`. O Prometheus já está a recolher métricas da webapp, do ClickHouse e do Uptime Kuma a cada 10 a 20 segundos.

### Passo 8: deixar um terminal a mostrar os alertas

Abre um segundo PowerShell, na mesma pasta, e deixa-o a correr:

```powershell
cd $HOME\shoplab-poc
docker compose logs -f alert-logger
```

**O que acontece:** o alert-logger é o destino final dos alertas. Quando uma regra do Prometheus dispara, o alerta passa pelo Alertmanager e aparece aqui como `[FIRING]`; quando o problema passa, aparece `[RESOLVED]`. Com tudo normal, este terminal fica calado.

## Explorar a stack

Com a stack a correr há 5 minutos, cada ferramenta já tem dados para mostrar. Os dashboards atualizam a cada 10 segundos.

### Grafana: dashboard "Atividade da Plataforma"

Lê diretamente as tabelas do ClickHouse e responde a "o que estão os utilizadores a fazer e o que está a falhar". Está dividido em cinco faixas:

- **Resumo do período:** pedidos, utilizadores autenticados, sessões, taxa de erro 5xx, latência p95 e receita.
- **Tráfego e utilizadores:** pedidos por segundo por classe de resposta (2xx/3xx, 4xx, 5xx), sessões ativas ao longo do tempo (vês a onda de 10 minutos do simulador), ações por tipo, o funil de conversão (viram produtos → carrinho → checkout → compra) e logins com sucesso versus falhados.
- **Erros e timeouts:** erros por tipo, latência p50/p95/p99, timeouts do gateway de pagamento e da pesquisa, uma tabela de desempenho por endpoint, sessões por país e dispositivos.
- **Logs:** os últimos 200 erros e avisos de `app_logs`, com hora, componente, rota, tipo e mensagem.
- **Produtos mais vistos:** visualizações, compras e receita por produto.

As mudanças de modo caos aparecem como linhas verticais vermelhas em todos os gráficos, para veres exatamente quando começou cada incidente.

### Grafana: dashboard "Infraestrutura"

Lê o Prometheus e responde a "como estão os serviços por dentro". Mostra o modo caos ativo, pedidos/s, taxa de erro, p95 e timeouts calculados a partir das métricas da webapp; o estado interno do ClickHouse (memória, queries/s, linhas inseridas, parts e merges); e o estado de cada monitor do Uptime Kuma, com os tempos de resposta. Abre-o em Dashboards → ShopLab.

### Uptime Kuma

Em http://localhost:3001 vês 15 monitores, todos verdes, verificados a cada 20 segundos:

- **7 contentores** (webapp, simulator, clickhouse, prometheus, alertmanager, alert-logger, grafana): ficam vermelhos se o contentor parar ou se o healthcheck falhar.
- **4 testes à plataforma:** a página inicial da loja; `/health`, que falha se a webapp perder a ligação ao ClickHouse; `/health/platform`, que falha se nos últimos 60 segundos a taxa de erros passar 10% ou a latência p95 passar 2 segundos; e "Logs a chegar ao ClickHouse", que pergunta ao ClickHouse se entraram pedidos no último minuto.
- **4 serviços de observabilidade:** ClickHouse, Prometheus, Alertmanager e Grafana.

A página de estado pública, em http://localhost:3001/status/shoplab, agrupa tudo em "Plataforma ShopLab", "Contentores" e "Observabilidade". É a vista que mostrarias a quem só quer saber se a plataforma está bem.

### Prometheus

Em http://localhost:9090 → Alerts vês as 11 regras de alerta e o estado de cada uma: verde (inactive), amarelo (pending, a condição é verdadeira mas ainda não passou o tempo mínimo) ou vermelho (firing). Em Graph podes testar queries, por exemplo a taxa de erros:

```promql
sum(rate(http_requests_total{status=~"5.."}[2m])) / sum(rate(http_requests_total[2m]))
```

### ClickHouse

O ClickHouse tem uma consola web em http://localhost:8123/play (utilizador admin, password admin123). Experimenta estas queries sobre os dados da plataforma.

Pedidos e erros por rota na última hora:

```sql
SELECT route, count() AS pedidos, countIf(status >= 500) AS erros_5xx,
       round(quantile(0.95)(duration_ms)) AS p95_ms
FROM platform.http_requests
WHERE ts > now() - INTERVAL 1 HOUR
GROUP BY route ORDER BY pedidos DESC;
```

O percurso completo de uma sessão que terminou numa compra (a query escolhe uma automaticamente):

```sql
SELECT ts, event, product_id, amount
FROM platform.user_events
WHERE session_id = (SELECT any(session_id) FROM platform.user_events WHERE event = 'purchase')
ORDER BY ts;
```

Os últimos erros com mensagem:

```sql
SELECT ts, logger, error_type, message
FROM platform.app_logs
WHERE level = 'ERROR'
ORDER BY ts DESC LIMIT 20;
```

### alert-logger

O alert-logger é o destino final dos alertas: um pequeno programa em Python (`alert-logger/alert_logger.py`, cerca de 30 linhas) que recebe os alertas e os escreve no terminal.

O caminho de um alerta é este:

1. Uma regra do Prometheus dispara, por exemplo a taxa de erros acima de 5%.
2. O Prometheus envia o alerta ao Alertmanager, que o agrupa e evita repetições.
3. O Alertmanager entrega-o ao alert-logger por webhook: um pedido HTTP para `http://alert-logger:5001/` com os detalhes em JSON.
4. O alert-logger lê esse JSON e imprime uma linha por alerta.

```
[FIRING] PlataformaTaxaErrosAlta severity=critical - Taxa de erros 5xx da ShopLab em 23.55% (limite 5%)
[RESOLVED] PlataformaTaxaErrosAlta severity=critical - Taxa de erros 5xx da ShopLab em 23.55% (limite 5%)
```

`FIRING` quer dizer que o problema começou e `RESOLVED` que passou. Nada fica guardado: as linhas existem apenas nos logs do contentor, que vês com `docker compose logs -f alert-logger`.

**Porque existe.** Num ambiente real, o Alertmanager enviaria os alertas por email, Slack, Microsoft Teams, Telegram ou para um sistema de piquete como o PagerDuty. Todos esses canais exigem contas, tokens ou um servidor de email. O alert-logger faz de substituto e permite ver o caminho completo de um alerta, da regra até à notificação, sem configurar nada externo.

**Trocar por um canal real.** Basta mudar o `alertmanager/alertmanager.yml`; o resto da stack fica igual. Para o Microsoft Teams, que o Alertmanager 0.27 suporta diretamente, o receiver ficaria assim:

```yaml
receivers:
  - name: logger
    msteams_configs:
      - webhook_url: https://o-teu-webhook-do-teams
        send_resolved: true
```

Aplica com `docker compose restart alertmanager`. Também podes manter o `webhook_configs` do alert-logger no mesmo receiver, ao lado do canal real, para teres os dois.

## Simular incidentes

O modo caos provoca um incidente de cada vez, e cada um é apanhado por pelo menos duas camadas da stack. Abre http://localhost:8080/admin e carrega no cartão do cenário, ou usa o script no PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\chaos.ps1 -Mode errors
```

O `-ExecutionPolicy Bypass` evita o bloqueio do Windows a scripts descarregados da internet, só para este comando.

| Modo | O que provoca | No Grafana | No Uptime Kuma | Alerta no alert-logger |
| --- | --- | --- | --- | --- |
| `errors` | 25% dos pedidos falham com erro 500 (deadlocks, exceções, pool de ligações esgotado) | Faixa vermelha 5xx sobe; "Erros por tipo" cheio de erro interno e erro BD; tabela de logs com as mensagens | "Saúde da plataforma" fica DOWN | `PlataformaTaxaErrosAlta` e `MonitorEmBaixo`, em \~1 a 2 min |
| `slow` | Todos os pedidos ganham 0,6 a 2,5 s de latência; metade das pesquisas fica lenta | p95 e p99 disparam; tempos de resposta dos monitores sobem | "Saúde da plataforma" fica DOWN (p95 acima de 2 s) | `PlataformaLenta` e `MonitorEmBaixo`, em \~2 a 3 min |
| `timeouts` | 70% das chamadas ao gateway de pagamento demoram mais de 2,5 s e dão erro 504 | Gráfico "Timeouts" sobe; funil cai entre checkout e compra; receita abranda | Em geral continua verde: só o checkout falha | `PlataformaTimeouts`, em \~1 a 2 min |
| `attack` | Bots fazem centenas de tentativas de login por minuto com passwords erradas, a partir de IPs e países estranhos | "Logins falhados" dispara; RU, CN e KP aparecem em "Sessões por país"; muitos 4xx | Continua verde: a plataforma responde bem | `PlataformaForcaBrutaLogin`, em \~1 a 2 min |
| `outage` | A "base de dados" da loja deixa de responder e toda a API devolve 503 | Quase tudo a vermelho; erros "BD indisponível"; compras a zero | "Saúde da plataforma" fica DOWN | `PlataformaTaxaErrosAlta` e `MonitorEmBaixo`, em \~1 min |
| `normal` | Volta ao funcionamento normal (cerca de 1% de erros) | Gráficos recuperam | Tudo verde após 1 a 2 verificações | `[RESOLVED]` para cada alerta, em 1 a 5 min |

Três testes que não usam o modo caos, mas sim o Docker:

- **Parar o simulador:** `docker compose stop simulator`. Ao fim de 1 a 2 minutos, o monitor "Logs a chegar ao ClickHouse" e o do contentor ficam DOWN, e chegam `MonitorEmBaixo` e `PlataformaSemTrafego`. A plataforma está de pé, mas ninguém a usa.
- **Parar o ClickHouse:** `docker compose stop clickhouse`. Caem o monitor do contentor, o do ClickHouse e o "API e ligação ao ClickHouse"; chega `ClickHouseDown`. Os dashboards da plataforma deixam de ter dados, mas a webapp guarda os registos em memória e grava-os quando o ClickHouse voltar.
- **Parar a webapp:** `docker compose stop webapp`. Quase todos os monitores da plataforma ficam vermelhos e chega `PlataformaInacessivel`.

No fim de cada teste, repõe com `docker compose start <serviço>` ou volta ao modo `normal`. Para ver o resumo de tudo num só comando, usa `powershell -ExecutionPolicy Bypass -File .\scripts\status.ps1`.

Uma nota sobre o desenho dos alertas: o Uptime Kuma não envia notificações próprias. O estado dos monitores é lido pelo Prometheus e os alertas seguem o mesmo caminho dos outros (Prometheus → Alertmanager → alert-logger), para haver um único sítio onde os alertas são definidos e entregues.

## Referência

Tudo o que precisas de consultar depois de a stack estar montada, num só sítio.

### Serviços, endereços e credenciais

| Serviço | Contentor | Endereço no PC | Credenciais | Papel |
| --- | --- | --- | --- | --- |
| Loja ShopLab | shoplab-webapp | http://localhost:8080 | — | Plataforma web que gera os dados |
| Modo caos | shoplab-webapp | http://localhost:8080/admin | — | Provocar incidentes |
| Simulador | shoplab-simulator | — | — | Utilizadores virtuais |
| ClickHouse | shoplab-clickhouse | http://localhost:8123/play | admin / admin123 | Guarda pedidos, eventos e logs |
| Grafana | shoplab-grafana | http://localhost:3000 | admin / admin123 | Dashboards |
| Uptime Kuma | shoplab-uptime-kuma | http://localhost:3001 | admin / ShopLab2026! | Monitorização de contentores e endpoints |
| Página de estado | shoplab-uptime-kuma | http://localhost:3001/status/shoplab | — | Estado público da plataforma |
| Prometheus | shoplab-prometheus | http://localhost:9090 | — | Métricas e regras de alerta |
| Alertmanager | shoplab-alertmanager | http://localhost:9093 | — | Encaminha os alertas |
| alert-logger | shoplab-alert-logger | — | — | Mostra os alertas recebidos |
| kuma-setup | shoplab-kuma-setup | — | — | Configura o Uptime Kuma e termina |

Utilizadores internos do ClickHouse: `shoplab_app` / `app123` (a webapp escreve com ele) e `grafana` / `grafana123` (só leitura, usado pelo Grafana e pelo Uptime Kuma). Todas as passwords são de POC: não exponhas estas portas fora do teu PC.

### Tabelas do ClickHouse (base de dados `platform`)

| Tabela | Uma linha por | Colunas principais |
| --- | --- | --- |
| `http_requests` | Pedido HTTP | ts, session\_id, user\_id, method, route, status, duration\_ms, country, device, error\_type |
| `user_events` | Ação de um utilizador | ts, session\_id, user\_id, event, product\_id, category, amount, search\_term |
| `app_logs` | Linha de log (INFO, WARN, ERROR) | ts, level, logger, message, error\_type, route, request\_id |

Os dados são apagados automaticamente ao fim de 7 dias. O schema está em `clickhouse/initdb/01_schema.sql` e só é aplicado quando o volume do ClickHouse está vazio: se o alterares, corre `docker compose down -v` antes de voltar a arrancar.

### Comandos do dia a dia

Todos corridos no PowerShell, dentro de `$HOME\shoplab-poc`:

| Para | Comando |
| --- | --- |
| Arrancar ou aplicar alterações | `docker compose up -d --build` |
| Ver o estado | `docker compose ps -a` |
| Resumo de saúde | `powershell -ExecutionPolicy Bypass -File .\scripts\status.ps1` |
| Mudar o modo caos | `powershell -ExecutionPolicy Bypass -File .\scripts\chaos.ps1 -Mode normal` |
| Seguir os alertas | `docker compose logs -f alert-logger` |
| Seguir o tráfego simulado | `docker compose logs -f simulator` |
| Ver os logs de um serviço | `docker compose logs <serviço>` |
| Mudar o volume de tráfego | Editar `USERS` no serviço `simulator` do `docker-compose.yml` e correr `docker compose up -d simulator` |
| Recarregar regras de alerta | `curl.exe -X POST http://localhost:9090/-/reload` |
| Parar tudo e manter os dados | `docker compose down` |
| Apagar tudo e recomeçar do zero | `docker compose down -v` |

### Resolução de problemas

| Sintoma | Causa provável | Solução |
| --- | --- | --- |
| O Docker Desktop não arranca ou fala em virtualização | Virtualização desligada na BIOS ou WSL 2 em falta | Ativar "Intel VT-x" ou "AMD-V" na BIOS; correr `wsl --install` como administrador e reiniciar |
| "port is already allocated" no arranque | Outra aplicação (ou o POC antigo) usa a porta | Parar a outra aplicação, ou mudar o número da esquerda da porta no `docker-compose.yml` |
| "container name is already in use" | Restos de um arranque anterior | `docker compose down` e voltar a arrancar |
| `shoplab-kuma-setup` em `exited (1)` | O Uptime Kuma ainda não estava pronto | `docker compose up -d kuma-setup` e ver `docker compose logs kuma-setup` |
| Monitores de contentores DOWN com erro de Docker | O Uptime Kuma não chega ao socket do Docker | Arrancar a stack a partir do PowerShell, não do Git Bash; confirmar com `docker exec shoplab-uptime-kuma ls -l /var/run/docker.sock` |
| Target `uptime-kuma` DOWN no Prometheus | API key em falta (kuma-setup falhou) | Correr de novo o kuma-setup (linha acima); o Prometheus apanha a chave sozinho |
| Grafana sem a datasource ClickHouse ou com "plugin not found" | O plugin não foi descarregado no arranque (sem internet ou proxy) | Confirmar a ligação à internet e correr `docker compose restart grafana` |
| Dashboard da plataforma vazio | Ainda não há dados, ou o simulador parou | Esperar 1 a 2 minutos; ver `docker compose logs simulator` e o passo 6 |
| Tudo lento ou contentores a reiniciar | Pouca memória para o Docker | Dar 6 GB ao WSL no `.wslconfig` (passo 2) ou baixar `USERS` para 10 |
| O script `.ps1` é bloqueado | Política de execução do Windows | Correr sempre com `powershell -ExecutionPolicy Bypass -File ...` |
