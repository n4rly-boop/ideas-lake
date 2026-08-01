# Ideas Lake

AIRI Summer 2026, проект 28 «Озеро идей».

Долговременная память между прогонами эволюции: статья, документация или лог прогона →
тезис → обезличенная карточка идеи. На чтение — запрос от эволюции → релевантные идеи
вместе с их тезисами и ссылками на источники.

Двухслойная модель. **Идея** — нода графа: обобщённая формулировка приёма, условия
применимости, ограничения, режимы отказа. **Тезис** — лист: до-обобщённое утверждение
близко к формулировке источника, с числом и провенансом. Дубль не выбрасывается — он
становится ещё одним листом, поэтому доверие к идее выводится из состава её листьев,
а не объявляется полем.

## Что здесь

| | |
|---|---|
| `lake/models.py` | `Source` / `Thesis` / `Idea` — pydantic; отдельно литеральные JSON-схемы для LLM |
| `lake/ingest/` | write path: fetch → parse → generalize → link → запись батчем |
| `lake/retrieve/` | read path: rewrite → гибридный поиск → подъём к идеям → ранжирование |
| `lake/research/` | reusable deep-research agent: Lake priors + independent web evidence → language report |
| `lake/api/` | HTTP-слой на всё: граф, поиск, ингест заданиями, retrieve, research и починка индекса |
| `lake/index.py` | индекс тезисов: SQLite FTS5 + вектора + RRF |
| `lake/graph_client.py` | единственное место, знающее формат графового хранилища |
| `lake/llm.py` | клиент llama.cpp: принуждение схемой, канарейка, fail-closed |
| `lake/prompts/` | промпты текстовыми файлами, не строками в коде |
| `lake/selfcheck.py` | assert-проверки инвариантов, один запуск |

Подробности реализации — [`lake/README.md`](lake/README.md): точки входа, слой API,
таблица тихих поломок и чем каждая закрыта.
Спека и разборы прототипов-доноров — в локальной базе знаний (`knowledge/`, в репозиторий
не входит).

Блок A (ingestion + retrieve) — этот код. Граф и Neo4j — блок B, эволюция — C, стоимость — D.

## Контракт наружу

```
POST /retrieve
  { query, k=5, run_id?, budget?, rewrite=true, allow_web=false }
->
  { ideas: [ { idea_id, text, applicability_conditions, limitations, failure_modes,
               effect_claimed, effect_observed, trust_score, score, cosine_similarity, via,
               theses: [ { text, url, title, effect, locator } ] } ],
    log_id, cost: { tokens_in, tokens_out, wall_ms } }
```

`via` — как идея попала в выдачу: `thesis` | `edge` | `padding`.

Отдельная ручка `POST /research` принимает естественно-языковой запрос и bounded
контекст, сначала получает приоры из `/retrieve`, а при включённом web независимо
ищет и читает источники через SearXNG, Crawl4AI и Docling. Она возвращает language
report с доступными URL и выдержками, а не готовые локальные карточки. Карточки
Lake используются только для gap/duplicate analysis и не копируются в task-local
memory. Состояние RAG и предупреждения явно возвращаются; если одновременно нет
рабочего RAG и independent web evidence, ответ — `503`, а не выдуманный пустой
успех.

Планирование и synthesis в `POST /research` используют Qwen3.6-35B-A3B.
Механические parse/generalize/retrieve-rewrite шаги остаются на Qwen3.5-9B,
а существующие link/trust judgement уже используют 35B.

Эволюционный `EvolutionResearchAgent` пока остаётся копией в
`gigaevo-core-runtime/` для совместимости текущих прогонов. Активная
интеграционная ветка Core вызывает `/research` из фонового research worker;
Core сам решает, какие
гипотезы передать в task-local memory. Ручка не вызывается из `select_cards`,
`pre_step_hook` или `post_step_hook`.

В RAG-only режиме (`LAKE_RESEARCH_WEB=0`) источник и grounded claim не являются
обязательными: отчёт использует bounded Lake priors, сохраняет доступные thesis
URL как untrusted provenance и оставляет independently fetched `sources=[]`.
Если structured synthesis 35B временно не прошёл контракт, найденные priors не
теряются: они возвращаются в отдельной явно untrusted секции отчёта. Любая
сформированная Core гипотеза всё равно остаётся `unverified` до эволюционной
проверки.

Это контракт C3, но не весь сервер. Тем же приложением ходят чтение графа
(`/sources`, `/ideas`, `/theses` — постранично, с фильтрами), сырой гибридный поиск
по тезисам (`/search`), ингест фоновыми заданиями (`/ingest/phase1|phase2`, слот один,
второй запуск → `409`), приём батчей эволюции (`/run`), очередь отказов арбитра
(`/ingest/pending-link`), судья доверия (`/admin/trust`), починка индекса
(`/admin/reindex`) и выгрузка озера в Obsidian-vault (`/vault/export`).
Скриптов, которые надо звать руками на машине с данными, не осталось.
Полный список — `GET /docs`, разбор — в [`lake/README.md`](lake/README.md#5-слой-api).

Всё то же вызывается импортом, не только по HTTP: составные операции живут в `lake/ops.py`,
выгрузка — в `lake/vault.py`, и оба ничего не знают про HTTP. Ручка — тонкая половина.

Ручки на запись тезиса нет: тезис неизменяем и создаётся только фазой 2, которая
назначает ему идею через арбитра. Удаления нет ни у чего.

FastAPI: `GET /docs` — Swagger, `GET /openapi.json` — машинная схема контракта,
`GET /healthz` — живость плюс сверка «индекс == хранилище».

Политика recall-first: отказа по низкому скору нет, выдача дозаполняется до `k`, но всё
выданное и всё отсечённое пишется в лог со скорами. Хранилище недоступно → `503`, а не
пустой `ideas`: «в озере ничего нет» и «озеро сломано» — разные вещи для замера
«с озером против без». Некорректный запрос → `400`, не 422.

## Запуск

Python 3.12. Зависимости: `fastapi`, `uvicorn`, `pydantic`, `numpy`,
`sentence-transformers`, `PyYAML`, `neo4j`, остальное — stdlib. Платных API нет: LLM — серверы
школы (llama.cpp), эмбеддинги считаются локально на CPU.

Секреты — только из окружения, в репозиторий не попадают:

```
LAKE_KEY_9B      ключ к Qwen3.5-9B
LAKE_KEY_35B     ключ к Qwen3.6-35B-A3B
LAKE_SEARXNG_URL  адрес локального SearXNG (по умолчанию http://127.0.0.1:8080)
LAKE_CRAWL4AI_URL адрес локального Crawl4AI (по умолчанию http://127.0.0.1:11235)
LAKE_DOCLING_URL  адрес локального Docling (по умолчанию http://127.0.0.1:5001)
LAKE_RESEARCH_TIMEOUT_S таймаут web/PDF-операции research (по умолчанию 30)
LAKE_RESEARCH_LLM_TIMEOUT_S таймаут одного 35B planning/synthesis-вызова (по умолчанию 90)
NEO4J_URI        neo4j+s://…
NEO4J_USERNAME
NEO4J_PASSWORD
NEO4J_DATABASE
```

```bash
python -m lake.selfcheck                      # инварианты; --offline снимает единственный сетевой пункт
python -m lake.api.selfcheck                  # офлайн-проверка HTTP-слоя, реальный data/ не трогает
python -m lake.research.selfcheck             # офлайн-проверка планирования, RAG/web boundary и отказа
python -m lake.api.app --port 8077            # сервер; --mock отдаёт форму /retrieve без графа и LLM
curl -H "Authorization: Bearer $LAKE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"query":"alternative mechanisms for maintaining diversity in evolutionary search"}' \
  http://127.0.0.1:8077/research
python -m lake.ingest.run phase1 --limit 3    # то же, что POST /ingest/phase1, из терминала
python -m lake.ingest.run phase2              # staging → граф + индекс, последовательно, с курсором
python -m lake.vault                          # озеро → data/vault, открыть папку как vault в Obsidian
python -m lake.neo4j_load --dry-run           # что уедет в Neo4j блока B; без флага — записывает
```

Приёмка стоит между фазами намеренно: тезисы читаются глазами из `staging.jsonl`, промпт
правится, фаза 1 перегоняется — и всё это до первой записи в граф.

### В контейнере

```bash
cp .env.local.example .env.local           # заполнить ключи
docker compose --env-file .env.local up -d                   # приложение + Neo4j
docker compose --env-file .env.local run --rm lake python -m lake.neo4j_load
docker compose down -v                                       # снести вместе с томом графа
```

`--env-file .env.local` нужен там, где приложению требуется окружение: compose по умолчанию
читает `.env`, а тот принадлежит запуску с хоста. Интерполяции в `docker-compose.yml` нет
намеренно, поэтому `down`, `ps` и `logs` работают и без флага — иначе погасить стек было бы
нельзя, не назвав файл.

Neo4j поднимается с `NEO4J_AUTH: none` и публикуется на `127.0.0.1` — одноразовая база на
петлевом интерфейсе, логин и пароль из `.env.local` она игнорирует. Сервис `lake` ждёт
здоровый Neo4j через `depends_on` и использует его как единственное графовое хранилище;
SQLite остаётся только для локального FTS-индекса и долговечной очереди заданий. Порт API
тоже привязан к `127.0.0.1`, но API защищён `LAKE_API_KEY`; `--no-auth` допустим только
для явно локального диагностического запуска.

Облачный граф — те же четыре `NEO4J_*` в `.env.local`, указывающие на Aura; менять код не
нужно, контейнер локального Neo4j в этом режиме можно не запускать. Для разовой загрузки
в отдельный граф используются `NEO4J_TARGET_*` через `lake.neo4j_load`, чтобы сервис чтения
не мог случайно залить данные сам в себя.

`lake/data` подключается bind-монтированием — результаты прогонов остаются на хосте. Модель
эмбеддингов запечена в образ: `create_app` греет энкодер до открытия порта, и качать её на
старте значит держать порт закрытым несколько минут.

Посмотреть граф: `python -m lake.vault`, затем в Obsidian «Open folder as vault» →
`lake/data/vault`. Идея, тезис и источник — по markdown-файлу, связи — обычные `[[wikilink]]`,
рисует Obsidian. Своего рендера нет и не будет, зависимостей ноль. Папка пересобирается
целиком каждым экспортом, правки в ней в озеро не возвращаются; `.obsidian/` с настройками
граф-вью переживает пере-экспорт. Раскрасить узлы по типу — три группы в настройках графа:
`path:"ideas/"`, `path:"theses/"`, `path:"sources/"`.

## Проверка и состояние

Исследовательский boundary проверяется без сети и ключей:
`python -m lake.research.selfcheck`. Для полного HTTP- и графового self-check
используйте собранный Docker-образ и пустой локальный Neo4j; CI запускает
`lake.ingest.run selfcheck`, `lake.api.selfcheck` и `lake.selfcheck --offline` именно
в таком окружении. Это важно: локальный Python без зависимостей FastAPI/Neo4j не является
валидной проверкой приложения, а self-check намеренно отказывается писать в непустой граф.

Текущие ограничения и точные контракты перечислены в
[`lake/README.md`](lake/README.md#8-известные-ограничения) и в `knowledge/`; live-run
evidence хранится рядом с артефактами соответствующего прогона, а не в этом README.
