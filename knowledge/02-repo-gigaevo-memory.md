# AIRI-Institute/gigaevo-memory — разбор репозитория

> Источник — публичное зеркало `AIRI-Institute/gigaevo-memory` (GitHub, `public`, `gh api repos/AIRI-Institute/gigaevo-memory --jq '.license'` → `null`, `stargazers_count` 3, `forks_count` 0, `description` не задано, `default_branch` `main`).
> Разбор сделан на коммит ветки `main` — единственный, `5452263e76e649315f578a0967562372aae69b49` («Release snapshot from release @ 36eb8a6», `2026-06-26 14:06:12 +0000`) — и ветку `origin/who-cares` @ `d3475c51b7cf935d1db7d1c3926389db750bb4e7` (`2026-06-11 18:12:41 +0300`, единственная remote-only ветка репозитория). Дата разбора: 2026-07-29.
> Обе даты старше контрольной точки 2026-07-26: `gh api repos/AIRI-Institute/gigaevo-memory --jq '.pushed_at'` отдаёт `2026-06-26T14:06:13Z` — репозиторий не сдвинулся ни на день после этой точки, последний пуш на 33 дня раньше даты разбора.

---

## TL;DR

1. **Главный вывод**: «memory» в названии репозитория означает **persistent store** — версионированное CRUD-хранилище артефактов CARL (`step`/`chain`/`agent`/`agent_skill`/`memory_card`) с поиском (BM25+vector+hybrid) и SSE-шиной событий, а не «agent memory» в смысле LLM-агентской памяти: по всему дереву репозитория «consolidat» — 0 совпадений, «forget» — 0, «knowledge.graph|graph.database|neo4j» — 0; «summariz/summaris» — 8 совпадений, и все относятся к служебной `_summarise()` в `diff_html.py:104,154` (подсчёт типов JSON-Patch операций для diff-рендера), не к какой-либо памяти.
2. **Масштаб кода и тестов**: во всём репозитории (`api/app`+`api/tests`+`web_ui`+прочее, `find . -name '*.py' | xargs wc -l`) — **29894** строки Python, это и есть основание округления «~30k» из известных фактов; сам `api/app` — только **10177** строк (`find api/app -name '*.py' | xargs wc -l`), заметно меньше «~30k». Тестовых функций в `api/tests` — **838**, если считать `def test_` и `async def test_` вместе; буквальный `grep`-паттерн без учёта `async` (`^def test_\|^    def test_`) даёт только **670**, недосчитывая ровно **168** асинхронных тестов (`pytest-asyncio`, `asyncio_mode = auto`, `api/pytest.ini:2`).
3. **Публикация через зеркало**: `main` — единственный orphan-коммит (`git log --oneline main | wc -l` → 1, поле `parents:` в `git log --format='%H parents:%P' main` пустое), собираемый workflow `.github/workflows/mirror.yml` из приватного `gigaevo-memory-internal` через его ветку `release`: триггер `workflow_run` по завершении CI на `release`, гейт `conclusion == 'success'` (`mirror.yml:37`), затем `git checkout --orphan public-snapshot` (`:74`) и форс-пуш секретом `MIRROR_TOKEN`, не `GITHUB_TOKEN` (`:56,61,80`). Имя приватного репозитория нигде не встречается в самом `mirror.yml` — оно всплывает только в прозе одного коммита `who-cares` (`c17f5d75`: «Sync updates from internal monorepo… Snapshot from gigaevo-memory-internal @ fdb14d0…»).
4. **Ветка `who-cares` и `TODO.md`**: `origin/who-cares` — 11 коммитов, явно случайно опубликованный срез реальной истории разработки; в ней лежит `TODO.md` на **2814** строк (`git show origin/who-cares:TODO.md | wc -l`). `grep -c '\[DONE' TODO.md` даёт **57** маркеров, и построчное чтение всех 9 разделов не находит ни одного пункта без `[DONE]` — весь документ отмечен как выполненный.
5. **`connected_ideas`**: `connected_ideas: list[dict[str, Any]]` (`api/app/models/requests.py:148`, дословно дублируется в `api/app/models/responses.py:61`) — единственное место во всём репозитории со словом «idea» (ровно 3 совпадения по всему дереву, третье — использование в поисковом документе `full_card`, `search_document_service.py:134`). Поле не типизировано глубже `dict[str, Any]`, не заполняется никаким сервисным кодом, не тестируется, не упомянуто ни в `docs/`, ни в `TODO.md`. Модель `MemoryCardContent`, где оно объявлено, сама **не используется нигде за пределами файла своего определения** (`grep -rn "MemoryCardContent" --include="*.py" .` — только объявления `requests.py:127` и `responses.py:40`, ни одного импорта; `memory_cards.py:14` работает с `content: dict[str, Any]` напрямую).
6. **Лицензии нет**: `README.md:248-250` заявляет «MIT» тремя словами без текста и без файла; `git show main:LICENSE` / `git show origin/who-cares:LICENSE` → `fatal: path 'LICENSE' does not exist`; `gh api repos/AIRI-Institute/gigaevo-memory --jq '.license'` → `null`; поле `license` отсутствует во всех трёх `pyproject.toml` (`api/`, `client/python/`, корневой).
7. **`ENABLE_VECTOR_SEARCH` выключен по умолчанию**: `settings.enable_vector_search: bool = False` (`config.py:10`). Без явного включения `POST /v1/search/unified` с `search_type in (vector, hybrid)`, `POST /v1/embeddings` и `GET /{entity_type}/duplicates` все отдают `503` (`routers/unified_search.py:96-101,189-192,240-243`; `routers/embeddings.py:63-66`; `routers/dedup.py:93-101` через `entity_service.py:1256-1257`), а `sync_entity_search_documents` просто не считает эмбеддинги для новых search-документов — BM25-индекс всё равно строится, vector-колонка остаётся `NULL` (`search_document_service.py:297-298`).
8. **Размерность вектора фиксируется в момент накатки миграции, а не в исходнике**: миграции 001 и 002 читают `settings.vector_dimension` (дефолт `384`, `config.py:11`) в f-строку SQL прямо на этапе `alembic upgrade` (`001_initial.py:107-110`, `002_memory_card_search_documents.py:107-112`) — смена `VECTOR_DIMENSION` в окружении и повторная накатка миграций на новой БД даёт другую размерность колонки без единой строки диффа в самой миграции.
9. **Дыра в авторизации на write/delete**: `Depends(require_api_key)` навешан только на POST-create и GET-list каждого типового роутера (`agent_skills.py:112,211`; `agents.py:50,127`; `chains.py:148,231`; `memory_cards.py:24,82`; `steps.py:65`, у которого даже create — без auth вообще) — ни один `PUT`/`PATCH`/`DELETE`/`favourite`/`run-recorded`/`lineage`/`versions/beating`/`revert`/`pin`/`promote` не требует ключа ни в одном из 13 файлов роутеров, включая `entities.py` и весь `versions.py`. Скоупы `write:any`, `delete:any`, `admin:keys`, `evolve` объявлены и включены в роли (`auth.py:66-97`), но ни разу не проверяются в коде (`grep -rn "has_scope(\|SCOPE_WRITE_ANY\|SCOPE_DELETE_ANY\|SCOPE_ADMIN_KEYS\|SCOPE_EVOLVE" api/app`, исключая `auth.py`, — 0 совпадений); единственный реально гейтящий вызов во всём приложении — `require_scope(SCOPE_CLEAR_ALL)` перед `POST /maintenance/clear-all` (`routers/entities.py:218`).

---

## История и способ публикации

### `main`

- `git log --oneline main | wc -l` → **1**. Единственный коммит `5452263e76e649315f578a0967562372aae69b49`, дата `2026-06-26 14:06:12 +0000`, сообщение `Release snapshot from release @ 36eb8a6`.
- `git log --format='%H parents:%P' main` — поле `parents:` пустое: коммит **orphan**, родителей нет.
- Механизм подтверждён файлом `.github/workflows/mirror.yml`:
  - Триггер — `workflow_run` по завершении workflow `CI` на ветке `release` приватного репозитория (`mirror.yml:15,18`: `workflow_run: … branches: [release]`).
  - Джоб продолжается только при `github.event.workflow_run.conclusion == 'success'` (`:37`) — красный CI на `release` до зеркала не долетает.
  - Чекаутится точный `head_sha` прогона CI (`:44`: `ref: ${{ github.event.workflow_run.head_sha || 'release' }}`), затем строится orphan-коммит (`:74`: `git checkout --orphan public-snapshot`) и форс-пушится (`:82-83`: `push --force … AIRI-Institute/gigaevo-memory.git public-snapshot:main`).
  - Пуш аутентифицирован секретом `MIRROR_TOKEN`, не `GITHUB_TOKEN` (`:56, 61, 80`) — комментарий в файле объясняет: `GITHUB_TOKEN` персистится как `http.extraheader` и перебивал бы `MIRROR_TOKEN`, роняя пуш с 403.
  - Имя приватного репозитория **не встречается в самом `mirror.yml`** (он живёт в приватном репо, шлёт наружу без самоописания) — но встречается в истории `who-cares`: коммит `c17f5d75f2663c1ddf3dae843a7c804bb49311fe`, сообщение `Sync updates from internal monorepo`, тело: «Snapshot from gigaevo-memory-internal @ fdb14d0: brings in API router and search-strategy refinements, drops the client/python SDK package…». Приватный репозиторий — **`gigaevo-memory-internal`**.
  - Снапшот тянется не напрямую из `gigaevo-memory-internal`, а из его ветки `release` (`mirror.yml:1-13`); имя приватного репозитория известно только по прозе одного коммита `who-cares`, а не по самому workflow-файлу, который себя не описывает.

### `origin/who-cares`

- `git log --oneline origin/who-cares | wc -l` → **11** коммитов. Ветка доступна только как remote-only — `git branch -a` не показывает `who-cares` среди локальных.
- Полный список (`git show --stat`):

| Хеш | Дата | Тема | Диф (`--stat`) |
|---|---|---|---|
| `780c0861` | 2026-04-06 17:13:26 +0300 | Initial commit | 1 файл, +21 |
| `9ccf30fd` | 2026-04-06 17:49:18 +0300 | v.0.1.0 release | 124 файла, +21824/−1 |
| `4457186b` | 2026-04-06 17:49:29 +0300 | v.0.1.0 release | 1 файл, +1/−1 |
| `c35da795` | 2026-05-08 16:51:01 +0300 | translate README | 1 файл, +45/−45 |
| `743163b2` | 2026-05-18 17:32:06 +0300 | prepare memory for CARE implementation | **144 файла, +26862/−500** |
| `20ed0453` | 2026-05-20 15:17:10 +0300 | small additions for CARE implementation | 8 файлов, +360/−5 |
| `da0f1b82` | 2026-06-09 16:46:13 +0300 | fix versions issue | 6 файлов, +179/−6 |
| `c17f5d75` | 2026-06-11 17:49:26 +0300 | Sync updates from internal monorepo | 109 файлов, +5173/−13513 |
| `6245a6b6` | 2026-06-11 17:52:31 +0300 | Drop stale gigaevo-client workspace member | 1 файл, −7 |
| `ee239edf` | 2026-06-11 17:57:44 +0300 | ci: install dev extras so pytest is available | 1 файл, +1/−1 |
| `d3475c51` | 2026-06-11 18:12:41 +0300 | update version | 6 файлов, +6/−6 |

- Коммит `743163b243ca7394e6bef1d9f0e29e5ab26addc0` «prepare memory for CARE implementation» (`2026-05-18 17:32:06 +0300`, **144 файла, +26862/−500**) — самый крупный по числу добавленных строк коммит ветки, если не считать `9ccf30fd` (+21824) — но тот является первым релизом, а не CARE-подготовкой.
- Автор коммитов везде указывается как «автор коммита» — персональные имена в этот разбор не выносятся.
- Последний коммит `who-cares` — `d3475c51`, `2026-06-11 18:12:41 +0300`. Git не хранит момент пуша отдельно от момента коммита; в качестве прокси для «последнего пуша» взята дата последнего коммита ветки. Разница с `main` (`2026-06-26`) — 15 дней: `who-cares` не обновлялась после этой даты, тогда как публичное зеркало продолжало обновляться (отдельными снапшотами `release`) ещё две недели без соответствующих изменений в `who-cares`.

---

## Ветка who-cares и TODO.md

`git show origin/who-cares:TODO.md | wc -l` → **2814** строк.

### Структура

- Заголовок (`TODO.md:1-14`): документ — implementation TODO для «CARE ecosystem», описывает GigaEvo Memory как хранилище CARL-артефактов (steps/chains/agents/memory cards) с иммутабельными версиями и channel pinning; три цели наверху — (a) AgentSkill как первоклассная сущность, (b) unified-клиент `gigaevo-client` (переименование `gigaevo-memory`), (c) quality-of-life для TUI CARE.
- Приоритеты (`TODO.md:10-12`): **P0** — блокер CARE MVP, **P1** — нужно для v0.1, **P2** — для полной экосистемы, **P3** — качество/полировка, **P4** — будущее/исследование.
- 9 разделов (H2, `TODO.md:16,644,1058,1526,1748,1951,2096,2304,2489`):
  1. AgentSkill как первоклассная сущность (P0)
  2. Unified GigaEvo client (P1)
  3. Auth & multi-user (P1–P2)
  4. Search & retrieval upgrades (P1–P2)
  5. Versioning & evolution metadata (P1–P2)
  6. SSE & real-time updates (P1)
  7. Operational & deployment (P2)
  8. Quality-of-life (P2–P3)
  9. Documentation (P2–P3)
  + «Cross-module dependencies» (таблица, `TODO.md:2795`) и «Suggested milestones» (`TODO.md:2807`).
- Формат пункта: `- **[DONE]** <описание задачи>`, дальше курсивом блок `*Shipped 2026-05-16 (iteration #N): …*` с перечнем изменённых файлов/строк, числом новых тестов и их разбиением по классам, и отдельным абзацем «Real-scenario/real-execution evaluation» с конкретным прогоном. Числа тестов — в форме `X/X pass`: `TODO.md:30` — «16/16 tests pass» (известный формат подтверждён буквально), далее десятки аналогичных: `TODO.md:1005` — «20 client-side + 85 server-side regression tests pass», `TODO.md:2431` — «286 server unit tests + 170 client unit tests pass».
- **Ключевая находка**: `grep -c '\[DONE' TODO.md` → **57** маркеров (включая варианты `[DONE for X; Y remains]`), и при построчном прочтении всех 9 разделов не нашлось ни одного пункта без `[DONE]` — весь документ, от §1 до §9, отмечен как выполненный. Единственные явные оговорки о недоделанном — не отдельные пункты, а подпункты внутри уже «DONE»-блоков:
  - `TODO.md:114` — `search_agent_skills` как отдельный метод: «deferred» (функциональность уже покрыта `SearchMixin.search(entity_type="agent_skill", …)`).
  - `TODO.md:292` — LRU-дедупликация `run_id` в `record_run`: «future-idempotency hook… deferred — see P1» (параметр `run_id` принимается, но не используется).
  - `TODO.md:884` — `cancel_evolution()`/`pause_evolution()`/`resume_evolution()` в `PlatformClient`: клиентские стабы, кидают `NotImplementedError`, «until gigaevo-platform ships the matching server routes».
  - `TODO.md:2121` — `pgvector_index_size` в `/health`: «deferred» (требует `pg_relation_size()` на каждый health-пробе, сочтено лишней нагрузкой).
  - `TODO.md:1063,1068,1115` — §3 (auth) изначально помечен `[DONE for foundation + writes-side wiring + make create-key; read-side scoping remains]` (итерация #25) — но чтение до конца раздела показывает, что read-side был закрыт итерацией #41 (`TODO.md:1337-1380`), и итоговая пометка (`TODO.md:1377`) гласит: «Closing this rollout means the §3 P1 spec is fully satisfied».
  - Ни одного открытого пункта, помеченного как ожидающий/непонятый/забракованный, не найдено.

### Связь с идеями / `connected_ideas`

`grep -n "connected_ideas\|idea" TODO.md` → **0** совпадений. `TODO.md` вообще не упоминает слово «idea» ни в каком написании — при том, что само поле `connected_ideas` существует в коде (`api/app/models/responses.py:61`, `api/app/models/requests.py:148`) и участвует в поисковой индексации (`search_document_service.py:134`). Похоже на артефакт другого клиентского контракта, протащенный через `content: dict[str, Any]` без официального описания на стороне Memory.

### `memory_card`

`grep -c "memory_card\|memory-card\|MemoryCard" TODO.md` → **24** упоминания:
- `memory_card` — один из исходных 4 типов сущностей (`TODO.md:18`, наряду с `step`/`chain`/`agent`), уже существовавший до `TODO.md`; сам документ не заводит для него новых задач как для первоклассной сущности (в отличие от `agent_skill`) — он участвует в перечислениях «применить то же самое к memory_cards» (`TODO.md:1314-1335` — auth/namespacing rollout; `:1349-1352` — namespaced list endpoint `GET /v1/memory-cards`, которого раньше не было вовсе; `:1957-1990` — SSE-события).
- `INDEXED_ENTITY_TYPES = {"memory_card", "agent_skill"}` (`TODO.md:87`) — только эти два типа индексируются в BM25/vector-поиск; `step`, `chain`, `agent` не индексируются документами вообще (импликация из `TODO.md`, не проговорена отдельно).
- `PlatformMemoryClient` изначально был «memory-card-only slim variant» (`TODO.md:647`) до unified-клиента §2.

### Поиск (в TODO.md)

Раздел 4 (`TODO.md:1526-1747`), все 5 пунктов `[DONE]`:
- `document_kind` для AgentSkill — 4 вида документов (`skill_description`, `skill_instructions`, `skill_full`, `skill_allowed_tools`), уже отгружено в итерации #4 (`TODO.md:1528-1537`).
- `find_capability_matches(rough_aim, top_k=3, …)` (`:1538-1569`) — BM25 по `skill_description` + опциональный «deep»-проход по `skill_instructions`, дедуп по `entity_id`.
- Reranker hook (`:1570-1617`) — `Reranker` Protocol, `IdentityReranker` по умолчанию, `RerankerRegistry`, конфиг `RERANKER_KIND`.
- Faceted-фильтр `requires_tool`/`excludes_tool` (`:1618-1661`) — пост-фильтр поверх JSONB `allowed_tools`, окно `min(limit*4, 200)`.
- Семантическая дедупликация `GET /v1/{entity_type}/duplicates` (`:1662-1744`) — pgvector `<=>` косинус, порог по умолчанию задаётся запросом (0.5–1.0), маршрут обязан монтироваться раньше типовых роутеров сущностей (`:1699-1706` — задокументированный баг, пойманный тестами).

### Эволюция (в TODO.md)

Раздел 5 (`TODO.md:1748-1949`), все 4 пункта `[DONE]`:
- Стандартизация `evolution_meta` (6 новых полей: `parent_version_ids`, `fitness_score`, `generation`, `experiment_id`, `objectives`, `mutation_kind`) с сохранением 5 legacy-полей gigaevo-core (`prompt_ref`, `fitness`, `is_valid`, `metrics`, `behavioral_descriptors`) бок о бок (`:1753-1785`).
- `GET /v1/chains/{id}/lineage` — BFS по `entity_versions.parents UUID[]`, дедуп по diamond-кроссоверам, `max_depth` 1–100 (`:1795-1835`).
- Канал `evolved` — auto-promotion правилами (нет fitness → no-op; нет канала → пин; текущий пин битый → перезаписать; новый fitness строго `>` старого → пин; иначе — не трогать), реализовано в `EntityService._maybe_promote_evolved_channel` (`:1836-1872`).
- `GET /v1/chains/{id}/versions/beating` — «promotion candidates» относительно `stable` (или произвольного канала/objective) (`:1873-1947`).

### CARE / MAGE / GigaEvo Platform

Пронизывает все 9 разделов; плотнее всего — §1 (AgentSkill целиком под нужды CARE/MAGE), §2 (unified-клиент, `PlatformClient` для gigaevo-platform), §5 (`evolution_meta` пишет платформа при каждом эволюционировавшем индивиде — таблица «Cross-module dependencies», `TODO.md:2802`: «§5 evolution_meta → Needed by Platform TODO §1»).
- `PlatformClient` (`TODO.md:797-885`) — 10 методов, покрывает `/api/v1/status`, эксперименты (`list/get/start/stop/get_status/get_results`), `create_chain_experiment`, `create_evolution`, `stream_events` (SSE). `GigaEvoSuite` держит `.memory` и `.platform` как композицию (не буквальное множественное наследование — ради раздельных `httpx.Client` на бэкенд).
- Таблица «Cross-module dependencies» (`TODO.md:2797-2803`) перечисляет потребителей: MAGE TODO §2/§5, CARE TODO §1.3/§3, `gigaevo-core`, `carl-mage`, Platform TODO §1/§2 — все внешние репозитории, не входящие в этот клон.
- Milestones (`TODO.md:2809-2814`): M0 (3 дня) — agent_skill backend+client; M0.5 (3 дня) — library metadata; M1 (неделя) — ingestion helper + web UI + auth P1; M2 (неделя) — client rename + `PlatformClient`; M3 (ongoing) — §4/5/6/7-9.

---

## Модель данных и хранение

Пять типов сущностей, единый реестр `VALID_ENTITY_TYPES` (`api/app/services/entity_service.py:31-37`): `steps→step`, `chains→chain`, `agents→agent`, `agent_skills→agent_skill`, `memory_cards→memory_card`. Общая ORM-схема — `api/app/db/models.py`.

- **`entities`** (`api/app/db/models.py:26-117`) — стабильная запись сущности: `entity_id` (UUID, PK), `entity_type` (`String(20)`, индекс), `namespace` (`String(255)`, индекс), `name`, `tags` (JSONB, `server_default="[]"`, GIN-индекс `ix_entities_tags`), `when_to_use` (Text), `channels` (JSONB, `server_default="{}"`, GIN-индекс `ix_entities_channels_active` под `deleted_at IS NULL`), `created_at`, `deleted_at`. `channels` — словарь `{имя_канала: version_id как строка}`, подтверждено в `entity_service.py:481` (`channels["latest"] = str(version_id)`) и `:1121/:1156` (`pin_channel`/`promote` просто переписывают ключ). Полнотекстовый `search_vector` (TSVECTOR) генерируется по `name`+`when_to_use`.
  - CARE-библиотечные поля, добавленные миграцией 003: `favourite` (Boolean, индекс), `run_count` (Integer), `last_run_at` (timestamptz, индекс), `display_name` (`String(200)`), `description` (Text) — `db/models.py:49-59`.
  - Индексы: `ix_entities_tags` (GIN), `ix_entities_type_created_entity_id_active` (частичный, `deleted_at IS NULL`), `ix_entities_channels_active` (GIN, частичный), `ix_entities_search_vector` (GIN), `ix_entities_library_listing` (`namespace, favourite, last_run_at`, частичный, миграция 003), `ix_entities_library_sort` (`namespace, last_run_at DESC NULLS LAST, entity_id`, частичный, миграция 005) — `db/models.py:74-117`.
- **`entity_versions`** (`db/models.py:148-172`) — неизменяемый снимок: `version_id` (PK), `entity_id` (FK), `version_number` (Integer, `default=0`), `content_json` (JSONB), `meta_json` (JSONB), **`parents`** (`ARRAY(UUID)`, nullable), `change_summary`, `evolution_meta` (JSONB), `author`, `created_at`. Колонка `embedding vector(N)` добавлена вне ORM-модели прямым SQL в миграции 001 — в `db/models.py` этого поля нет вовсе как `Mapped`-атрибута (расхождение ORM/схема).
- **`entity_search_documents`** (`db/models.py:175-218`, миграция 002) — производные документы для поискового индекса: `document_id`, `entity_id`/`version_id` (FK), `entity_type`, `namespace`, `document_kind`, `card_id`, `text_content`, `meta_json`, плюс `embedding` и generated `search_vector` (тоже добавлены SQL-миграцией, тоже не в ORM-модели). Уникальный индекс `(entity_id, version_id, document_kind)`.
- **`api_keys`** (`db/models.py:120-145`, миграция 004) — хранит только SHA-256-хэш ключа (`key_hash`, unique+индекс), `owner`, `label`, `scopes` (JSONB), `created_at`/`expires_at`/`revoked_at`.
- **Версионирование**: `version_number` — счётчик 0-based, первая версия всегда `0` (`entity_service.py:319`, комментарий «First version is v0»), следующая — `COUNT(*)` существующих версий (`:446/:468`).
- **`parents`**: колонка `ARRAY(UUID)`, но всегда получает не больше одного элемента — и на создании (`entity_service.py:307`: `parents = [uuid.UUID(parent_version_id)] if parent_version_id else None`), и на обновлении (`:451-452`), и на revert (`:1097`, `parent_version_id=str(target_version_id)`). Ни один путь не пишет 2+ родителя в эту колонку, хотя тип — массив.
- **Каналы/namespace**: канал — произвольная строка-ключ в `entities.channels`, дефолт везде `"latest"`; `namespace` — плоское поле `String(255)`, используется для auto-scoping (`api/app/auth.py:143-210`).
- **`VECTOR_DIMENSION` фиксируется не литералом, а на этапе применения миграции** — обе миграции 001 и 002 читают `settings.vector_dimension` (`config.py:11`, дефолт `384`) в f-строку SQL: `api/app/db/migrations/versions/001_initial.py:107-110` (`ALTER TABLE entity_versions ADD COLUMN embedding vector({settings.vector_dimension})`), `002_memory_card_search_documents.py:107-112` (тот же паттерн для `entity_search_documents`). Значит, размерность колонки — это значение `VECTOR_DIMENSION`/дефолт `384` **в момент запуска `alembic upgrade`** на конкретном окружении, а не константа, зашитая в файл миграции: смена `VECTOR_DIMENSION` и повторная накатка миграций 001/002 на новой БД даст другую размерность колонки без единой строки диффа в самой миграции.

---

## Схемы контента

Все четыре схемы контента определены дважды: полностью, с валидаторами и докстрингами, в `api/app/models/requests.py`, и частично — зеркально — в `api/app/models/responses.py` (только `MemoryCardContent`, `Strategy`, `EvolutionStatistics`, `MemoryCardUsage`, `MemoryCardExplanation`; `responses.py:10-61` дословно повторяет `requests.py:17-22,105-148` как отдельно определённые классы, не общие/не унаследованные).

Ниже — дословный уцелевший фрагмент первой редакции этого разбора (строки 410–449), с ревизией от 2026-07-29 по итогам независимого прочтения `api/app/models/requests.py`. Фрагмент начинается посреди утерянного кодового блока — открывающего ограждения ` ``` ` в уцелевшем куске нет, только закрывающее:

>     increased_fitness: float | None = None
>
> class Strategy(str, Enum):                   # requests.py:17
>     EXPLORATION = "exploration"
>     EXPLOITATION = "exploitation"
>     HYBRID = "hybrid"
> ```
>
> **Ключевое наблюдение для Проекта 28**: `connected_ideas: list[dict[str, Any]]` — единственное место во всём репозитории, где встречается слово «idea». Поле объявлено, включается в поисковый документ `full_card` (`search_document_service.py:134`), но **нигде не типизировано и нигде не заполняется** — ни валидации, ни примеров, ни тестов, ни упоминаний в `docs/` и `TODO.md`. Это пустой слот, оставленный ровно под то, чем занимается Ideas Lake.
>
> **`EvolutionMeta`** — `api/app/models/requests.py:33`. Две концентрические схемы, все поля опциональны, пустой `EvolutionMeta()` легален:
>
> | Поле | Тип | Схема |
> |---|---|---|
> | `parent_version_ids` | `list[str] \| None` | CARE/Platform (P1 §5, стандартизовано 2026-05-16). Мутация → длина 1, кроссовер → ≥ 2 |
> | `fitness_score` | `float \| None` | CARE/Platform. Диапазон зависит от fitness-функции |
> | `generation` | `int \| None` (`ge=0`) | CARE/Platform |
> | `experiment_id` | `str \| None` | CARE/Platform, идентификатор эксперимента gigaevo-platform |
> | `objectives` | `dict[str, float] \| None` | CARE/Platform, мультиобъектив: `{"accuracy": 0.91, "latency_ms": 1240, "tokens": 4200}` |
> | `mutation_kind` | `str \| None` | общее. Типичные: `step_swap`, `prompt_rewrite`, `topology_change`, `crossover`, `manual_edit` |
> | `prompt_ref` | `str \| None` | legacy gigaevo-core |
> | `fitness` | `float \| None` | legacy alias для `fitness_score` |
> | `is_valid` | `bool \| None` | legacy |
> | `metrics` | `dict[str, Any] \| None` | legacy, свободный мешок метрик |
> | `behavioral_descriptors` | `dict[str, Any] \| None` | legacy, **блок behavioural descriptors из MAP-Elites** |
>
> **`AgentSkillContent`** — `api/app/models/requests.py:244`: `name` (1–200), `description`, `uri`, `sha256` (64 hex, pattern `^[0-9a-fA-F]{64}$`), `manifest: dict`, `instructions: str` (тело SKILL.md), `allowed_tools: list[str]`, `tags`, `compatibility`, `tarball_url`, `tarball_sha256`. Формы `uri`: `github://owner/repo[/subpath][@ref]`, `local://…`, `https://…`, `module://pkg`, голое имя.
>
> **`CareChainMetadata`** — `api/app/models/requests.py:175`, блок внутри `chain.content_json["metadata"]`: `task_description`, `context_files: list[ContextFileRef]`, `generated_by` (`"mage"`/`"user"`), `mage_metadata: dict` (полный `MAGEMetadata.model_dump()`: `domain`, `num_steps`, `stages_completed`, `generation_time_seconds`), `display_name` (≤200), `description`, `tags`.
> Два хелпера: `from_chain_content(content)` (`:214`) — типизированное представление, игнорирует чужие ключи, возвращает пустой инстанс при отсутствии блока; `merge_into_content(content)` (`:229`) — **сохраняет чужие ключи** внутри `metadata`, чтобы несколько клиентов сосуществовали.
>
> **`ContextFileRef`** — `api/app/models/requests.py:151`: `path`, `sha256` (64 hex, обязателен), `size_bytes` (`ge=0`), `mime_type`.

*[ревизия 2026-07-29: часть A подтверждает адрес `requests.py:33` для `EvolutionMeta`, но уточняет его до диапазона `:33-102`, с точной разбивкой CARE/Platform-полей на `:39-48,57-84` и legacy-полей на `:49-52,86-102`. **Расхождение с уцелевшим фрагментом**: этот фрагмент относит `mutation_kind` к категории «общее» (не CARE/Platform и не legacy явно), тогда как часть A классифицирует `mutation_kind` как legacy gigaevo-core наравне с `prompt_ref`/`fitness`/`is_valid`/`metrics`/`behavioral_descriptors` (`requests.py:49-52,86-102`) — источники расходятся в том, к какой из двух схем принадлежит это поле; ни один из них этого расхождения явно не проговаривает. Часть A добавляет находку не из уцелевшего фрагмента: `parent_version_ids` документирован как поддерживающий 2+ родителей для кроссовера, но реальная колонка `entity_versions.parents`, которую обходит `/lineage` (`chains.py:450`), заполняется исключительно из одиночного `parent_version_id: str | None` (`requests.py:328,337,379`) — multi-parent происхождение декларировано в схеме, но никогда не долетает до колонки (см. раздел «Модель данных и хранение»).]*

*[ревизия 2026-07-29: часть A уточняет `AgentSkillContent` до диапазона `requests.py:244-319`, с точным адресом валидатора `sha256` — `^[0-9a-fA-F]{64}$` на `:276-282`, `name`: `min_length=1, max_length=200`. Число полей (11, включая `compatibility`/`tarball_url`/`tarball_sha256`) и 4 формы `uri` согласуются между источниками без противоречий; независимо подтверждается `docs/AGENT_SKILL_ENTITY.md` — «11 полей, 4 обязательных».]*

*[ревизия 2026-07-29: часть A уточняет `CareChainMetadata` до диапазона `requests.py:175-241`, хелперы — `from_chain_content` `:214-227`, `merge_into_content` `:229-241`, оба протестированы (`test_care_chain_metadata.py:82-171`). Без противоречий с уцелевшим фрагментом.]*

*[ревизия 2026-07-29: часть A уточняет `ContextFileRef` до диапазона `requests.py:151-172`, с точными строками валидаторов: `sha256` — `min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$"` (`:160-166`), `size_bytes: int = Field(..., ge=0)` (`:167`). Без противоречий.]*

*[ревизия 2026-07-29: про `connected_ideas` часть A и часть C независимо подтверждают счёт — ровно **3** совпадения слова «idea» на весь репозиторий (`requests.py:148`, `responses.py:61`, `search_document_service.py:134`), и добавляют находку за пределами уцелевшего фрагмента: вся модель `MemoryCardContent`, где объявлено поле, **не используется нигде за пределами файла своего определения** — `grep -rn "MemoryCardContent" --include="*.py" .` даёт только объявления `requests.py:127` и `responses.py:40`, ни одного импорта; `memory_cards.py:14` импортирует лишь `EntityCreateRequest, EntityUpdateRequest` и работает с `content: dict[str, Any]` напрямую.]*

Вспомогательные модели `EvolutionStatistics`/`MemoryCardUsage`/`MemoryCardExplanation` (`requests.py:105-124`) — без валидаторов границ; `best_quartile` — незашитая строка `"Q1"`–`"Q4"` в комментарии, не `Enum`/`Literal`.

---

## Маршруты и поверхность API

13 файлов роутеров.

### Таблица маршрутов

| Путь | Методы | Тип сущности | Особые ручки |
|---|---|---|---|
| `/health` | GET | — | `health.py:76` |
| `/metrics` | GET | — | `metrics.py` |
| `/v1/steps` | POST, GET | step | нет PATCH/favourite/run-recorded/lineage |
| `/v1/steps/{id}` | GET, PUT, DELETE | step | — |
| `/v1/steps/{id}/versions[/…]` | GET | step | typed-алиас, недостижим |
| `/v1/chains` | POST, GET | chain | — |
| `/v1/chains/{id}` | GET, PUT, PATCH, DELETE | chain | `chains.py:358` |
| `/v1/chains/{id}/favourite` | POST | chain | `:392` |
| `/v1/chains/{id}/run-recorded` | POST | chain | `:414` |
| `/v1/chains/{id}/lineage` | GET | chain | `:440` |
| `/v1/chains/{id}/versions/beating` | GET | chain | `:478` (дифференциальный канал) |
| `/v1/chains/{id}/versions[/…]` | GET | chain | typed-алиас, недостижим |
| `/v1/agents` | POST, GET | agent | — |
| `/v1/agents/{id}` | GET, PUT, PATCH, DELETE | agent | `agents.py:243` |
| `/v1/agents/{id}/favourite`, `/run-recorded` | POST | agent | `:278`, `:300` |
| `/v1/agents/{id}/versions[/…]` | GET | agent | typed-алиас, недостижим |
| `/v1/agent-skills` | POST, GET | agent_skill | доп. фильтры `requires_tool`/`excludes_tool` |
| `/v1/agent-skills/{id}` | GET, PUT, PATCH, DELETE | agent_skill | `agent_skills.py:337` |
| `/v1/agent-skills/{id}/favourite`, `/run-recorded` | POST | agent_skill | `:371`, `:393` |
| — нет `/v1/agent-skills/{id}/versions` typed-алиаса — | | | только через generic `/v1/{type}/{id}/versions` |
| `/v1/memory-cards` | POST, GET | memory_card | **нет PATCH/favourite/run-recorded/lineage** |
| `/v1/memory-cards/{id}` | GET, PUT, DELETE | memory_card | — |
| `/v1/memory-cards/{id}/versions[/…]` | GET | memory_card | typed-алиас, недостижим |
| `/v1/bulk/save` | POST | mixed | `bulk.py:133`, до 500 items, per-item error isolation |
| `/v1/search/unified`, `/vector`, `/batch` | POST | — | `unified_search.py:41,176,212` |
| `/v1/search/facets` | GET | — | `:282`, `authors` всегда `{}` |
| `/v1/embeddings` | POST | — | `embeddings.py:37`, 503 если `ENABLE_VECTOR_SEARCH=false` |
| `/v1/events/stream` | GET (SSE) | — | `events.py:99` |
| `/v1/webhooks`, `/v1/webhooks/{id}` | POST, DELETE | — | **501** (`events.py:175,181`) |
| `/v1/{entity_type}` | POST | generic (5 типов) | `entities.py:26`, «deprecated» |
| `/v1/{entity_type}/{id}` | GET, PUT, DELETE | generic | `entities.py:68,102,160` |
| `/v1/maintenance/clear-all` | POST | generic | `entities.py:181`, скоуп `clear:all` + `X-Confirm` |
| `/v1/{entity_type}/duplicates` | GET | все 5 типов | `dedup.py:26`, 503 без `ENABLE_VECTOR_SEARCH` |
| `/v1/{entity_type}/{id}/versions[/…]` | GET | generic | `versions.py:29,59` |
| `/v1/{entity_type}/{id}/diff` | GET | generic | `:89`, `?format=html` |
| `/v1/{entity_type}/{id}/revert`, `/pin`, `/promote` | POST | generic | `:135,166,182` |

### Второсортные типы сущностей

`memory_cards.py` объявляет ровно пять маршрутов — `POST ""` (`:21`), `GET ""` (`:70`), `GET "/{id}"` (`:128`), `PUT "/{id}"` (`:163`), `DELETE "/{id}"` (`:217`) — без `PATCH`/`favourite`/`run-recorded`/`lineage`. `steps.py` — тот же набор из пяти: `:16, :57, :114, :149, :203`. Для сравнения, `agents.py` и `agent_skills.py` добавляют к тем же пяти ещё `PATCH` + `favourite` + `run-recorded` (`agents.py:243,278,300`; `agent_skills.py:337,371,393`), а `chains.py` — ещё и `/lineage` (`:440`) и `/versions/beating` (`:478`), которых нет вообще ни у кого другого. `/duplicates` — единственная «расширенная» ручка, доступная всем пяти типам (`dedup.py:26` работает через общий `VALID_ENTITY_TYPES`, `entity_service.py:31-37`). Для Ideas Lake, где карточка идеи соответствует `memory_card`, это прямой пробел: релевантный проекту тип — второсортный по набору ручек.

`agent_skills` тоже не имеет `/lineage` — только у `chains` (`docs/EVOLUTION_META.md:221-223`: «Same shape can be added to other typed routers if MAGE/CARE start evolving agents, agent_skills, or memory_cards»).

### Недостижимые типизированные алиасы версий

`versions.py:206-281` — восемь typed-алиасов (`list_step_versions`, `list_chain_versions`, `list_agent_versions`, `list_memory_card_versions`, `get_step_version`, `get_chain_version`, `get_agent_version`, `get_memory_card_version`). Они объявлены в том же роутере после generic `GET /{entity_type}/{entity_id}/versions` (`:29-56`) и `GET /{entity_type}/{entity_id}/versions/{version_id}` (`:59-86`). FastAPI/Starlette матчит маршруты в порядке регистрации внутри роутера; строка пути `/steps/{step_id}/versions` тривиально подходит под шаблон `/{entity_type}/{entity_id}/versions` (`entity_type="steps"`, `entity_id` парсится как UUID) и матчится первым — восемь typed-алиасов физически никогда не исполняются. Для `agent_skill` такого алиаса вообще нет — из четырёх типов, у которых в файле есть typed-версийные ручки (steps/chains/agents/memory-cards), ни одна не работает; работает только generic-путь. `openapi.yaml` при этом документирует все восемь как отдельные рабочие пути (например `/v1/steps/{step_id}/versions` на строке 4461) без единого сигнала о мёртвом коде.

### Порядок монтирования роутеров (`main.py:65-100`)

1. `health.router`, `metrics_router` — без `/v1` (`:66-67`).
2. `dedup.router` (prefix `/v1`) — до типовых роутеров, иначе `/v1/{type}/{entity_id}` (generic) распарсит литерал `duplicates` как UUID и вернёт 422 вместо списка дублей (`:69-72`; подтверждено — `entities.py:71` типизирует `entity_id: uuid.UUID`).
3. `steps.router`, `chains.router`, `agents.router`, `agent_skills.router`, `memory_cards.router` (`:76-80`) — у каждого фиксированный литеральный prefix, коллизий с wildcard-роутами нет.
4. `bulk.router` (prefix `/v1`, `:83`).
5. `unified_search.router`, `embeddings.router` (prefix `/v1`, `:88-89`) — до `entities.router`, иначе `/v1/search/facets` разобрался бы как `entity_type="search", entity_id="facets"`.
6. `events.router` (prefix `/v1`, `:94`) — до `entities.router` по той же причине (`/events/stream`, `/webhooks`).
7. `entities.router` (generic, deprecated, `:97`).
8. `versions.router` (`:100`) — последний.

`routers/__init__.py:3-16` экспортирует только 10 из 13 модулей (`agents, chains, embeddings, memory_cards, entities, events, health, steps, unified_search, versions` — без `agent_skills`, `bulk`, `dedup`), но это не влияет на реальную маршрутизацию: `main.py:12-26` делает прямой `from .routers import (agent_skills, agents, bulk, chains, dedup, embeddings, entities, events, health, memory_cards, steps, unified_search, versions)` — импорт подмодуля работает независимо от `__all__` (тот влияет только на `from package import *`, а такой формы импорта в репозитории нигде нет). Несовпадение 10 vs 13 — косметическая нестыковка в `__init__.py`, не функциональная дыра.

### Расхождения `openapi.yaml` (5111 строк, `openapi: 3.1.0`, `info.version: 0.1.1`) с кодом

- **`MemoryCardContent`, `AgentSkillContent`, `CareChainMetadata`, `ContextFileRef` отсутствуют из `openapi.yaml` целиком** (`grep -n "MemoryCardContent\|AgentSkillContent\|CareChainMetadata\|ContextFileRef" openapi.yaml` — 0 совпадений на все четыре имени). Причина структурная: маршруты принимают/отдают `content: dict[str, Any]` (например `EntityCreateRequest.content`, `requests.py:325`) — типизированные модели никогда не фигурируют как аннотация поля в модели запроса/ответа, а FastAPI включает в OpenAPI только схемы, реально используемые в сигнатурах эндпоинтов. Для сравнения, `EvolutionMeta` действительно вписан в `openapi.yaml` (строки 332, 722, 884, 899, 914, 989) — потому что реально типизирует поле `evolution_meta` в `EntityCreateRequest`/`EntityUpdateRequest`/`BulkSaveItem`.
- **Восемь typed-версийных путей документированы как живые** без пометки, что они недостижимы (см. выше).
- `entity_versions.embedding` и `entity_search_documents.embedding`/`search_vector` — колонки существуют в БД (добавлены прямым SQL в миграциях 001/002), но отсутствуют как `Mapped`-поля в `api/app/db/models.py` (`EntityVersion`, `:148-172`, и `EntitySearchDocument`, `:175-218`) — расхождение ORM-модели с миграциями, отдельное от расхождений с `openapi.yaml`.
- `POST /v1/webhooks`, `DELETE /v1/webhooks/{webhook_id}` документированы в `openapi.yaml:4544-4583` с единственным ответом `'501': description: Successful Response` — автоген FastAPI подписывает основной объявленный `status_code` ярлыком «Successful Response» независимо от того, что 501 — код отказа; в спеке нет отдельного текста, что это заглушка (хотя в `description` эндпоинта сказано «not yet implemented»).

---

## Поиск

### Абстракции

- `SearchStrategy` (`search_strategies/base.py:183-221`) — ABC с двумя абстрактными методами: `async def search(self, request: SearchRequest) -> list[SearchHit]` и `async def batch_search(self, request: SearchRequest, queries: list[str]) -> list[list[SearchHit]]`. Конструктор принимает `db: AsyncSession`, заводит `self.context = SearchContext(db)` (`:196`).
- `SearchContext` (`base.py:97-180`) — держит `db`, даёт `build_filters()` и `format_hit()`. Мёртвый код: `self.context` нигде не читается ни в `BM25SearchStrategy`, ни в `VectorSearchStrategy`, ни в `HybridSearchStrategy` — все три сами строят SQL и `SearchHit` инлайн; единственное упоминание `self.context` во всём пакете — само присваивание в `base.py:196`.
- `EmbeddingBackend` (`embedding_service.py:17-51`) — ABC: `async def embed(self, texts: list[str]) -> list[list[float]]` (абстрактный), `dimension` — абстрактное свойство `int`, плюс конкретный `embed_query`. Три реализации: `SentenceTransformersBackend` (`:54`, дефолт, ленивая загрузка модели в threadpool), `OpenAIBackend` (`:117`), `HuggingFaceBackend` (`:175`). Выбор бэкенда — не реестр, а `if/elif` по `settings.embedding_provider` в `EmbeddingService._create_backend` (`:280-317`): `"sentencetransformers"`/`"openai"`/`"huggingface"`, иначе `ValueError`.
- `Reranker` (`search_strategies/reranker.py:41-53`) — `Protocol` (не ABC), `runtime_checkable`, один метод `async def rerank(self, query: str | None, hits: list[SearchHit]) -> list[SearchHit]`.

### Реранкер: зарегистрирован только identity

`RerankerRegistry._factories` (`reranker.py:82`) — на момент импорта модуля содержит единственную запись `{"identity": IdentityReranker}` (регистрация на `:124`). Cross-encoder не поставляется — `grep -rni "cross.encoder\|crossencoder" api/app` находит только упоминания в докстрингах/комментариях (`config.py:37`, `reranker.py:5,77`), ни одного класса. `RerankerRegistry.get()` (`:90-108`) при неизвестном `kind` логирует warning и возвращает `IdentityReranker()`. `settings.reranker_kind` (`config.py:40`) по умолчанию `"identity"`.

### Три стратегии

- **BM25** (`bm25_strategy.py`) — `func.websearch_to_tsquery("english", request.query)` (`:45`), ранжирование `func.ts_rank_cd(Entity.search_vector, tsquery, 32)` (`:48-52`). Комментарий на строке 51 гласит «Normalization: divide by document length + 1» — это неточно: флаг `32` в `ts_rank_cd` PostgreSQL означает «делить ранг на (ранг + 1)» (`rank/(rank+1)`), не деление на длину документа. Фильтры: `deleted_at IS NULL`, `entity_type`, полнотекстовый `@@`, опционально `namespace`, `tags` (через `Entity.tags.contains([tag])` — сравнение «содержит», не JSONB `?&`, в отличие от `list_entities`). При наличии `default_bm25_document_kind(entity_type)` (`memory_card`/`agent_skill`) идёт путь `_search_indexed_documents` (`:117-211`), джойнящий `entity_search_documents` и считающий `ts_rank_cd` по `esd.search_vector` с той же нормализацией `32`.
- **Vector** (`vector_strategy.py`) — pgvector `<=>` (косинусное расстояние), скор = `1 - distance` (`:89, :184`). Guard `vector_dims(embedding) = :vector_dimension` (`:60, :148`) — версии с эмбеддингом иной размерности молча отфильтровываются, а не 500-ят. `batch_search` для чистого vector-стратегии не реализован по-настоящему — явно бросает `ValueError` (`:246-249`): «Vector batch search requires pre-computed query vectors», расчёт на то, что вызывающий код (`UnifiedSearchService.batch_search`) заранее батчево эмбеддит запросы и никогда не зовёт `VectorSearchStrategy.batch_search` напрямую.
- **Hybrid** (`hybrid_strategy.py`) — запускает BM25 и Vector последовательно, не `asyncio.gather` (`:88-89`; комментарий `:57-58`: «sharing no concurrent DB work» — обе используют один `AsyncSession`). Оба под-запроса тянут `top_k * 2` (`:62, :77`) «для лучшего слияния». Нормализация — min-max на `[0,1]` по каждому списку раздельно (`_normalize_scores`, `:107-132`); если все скоры равны — всем ставится `0.5`. Веса `hybrid_weights: tuple[float, float] = (0.5, 0.5)` (`base.py:37`) нормализуются на сумму=1 перед смешиванием (`:50-55`). Хиты, встретившиеся только в одной из двух выдач, получают скор `score * weight_этой_стороны` без штрафа за отсутствие другой части.

**Мёртвые настройки**: `settings.hybrid_default_bm25_weight` / `hybrid_default_vector_weight` (`config.py:22-23`, оба `0.5`) объявлены, но нигде не читаются (`grep -rn` даёт только строку определения) — реальный дефолт `(0.5, 0.5)` жёстко зашит в `SearchRequest.hybrid_weights` (`base.py:37`) и в `UnifiedSearchRequest`/`BatchSearchRequest` (`models/requests.py:506,580`).

### `ENABLE_VECTOR_SEARCH`

`settings.enable_vector_search: bool = False` (`config.py:10`) — дефолт выключен. При выключенном флаге:
- `POST /v1/search/unified` с `search_type in (vector, hybrid)` → `HTTPException(503, "Vector search is not enabled. Set ENABLE_VECTOR_SEARCH=true.")` (`routers/unified_search.py:96-101`, то же на `:189-192` и `:240-243` для facets/batch);
- `POST /v1/embeddings` → тот же `503` (`routers/embeddings.py:63-66`);
- `GET /{type}/duplicates` → `EntityService.find_duplicate_pairs` возвращает `None` (`entity_service.py:1256-1257`), роутер превращает в `503` (`routers/dedup.py:93-101`);
- `sync_entity_search_documents` (`search_document_service.py:297-298`) просто не считает эмбеддинги для новых search-документов — BM25-индекс всё равно строится, vector-колонка остаётся `NULL`.

---

## Виды поисковых документов

`search_document_service.py`. Два индексируемых типа: `INDEXED_ENTITY_TYPES = {"memory_card", "agent_skill"}` (`:52`).

### `memory_card` — 6 видов (`derive_memory_card_search_documents:203`, `raw_docs` на `:214-225` — ровно 6 ключей)

| `document_kind` | Содержимое | Адрес |
|---|---|---|
| `full_card` | `_build_full_card_text(content)` — конкатенация `id, category, description, task_description_summary, task_description, program_id, fitness, strategy, last_generation, programs, aliases, keywords, evolution_statistics, explanation_summary, explanation_full, works_with, links, connected_ideas, usage, code` построчно `"поле: значение"`, пустые поля выбрасываются | `:114-138, :215` |
| `description` | `content["description"]` как есть | `:216` |
| `task_description` | `task_description`, при пустом — фоллбэк на `task_description_summary` | `:217` |
| `explanation_summary` | `_explanation_parts(content)[0]` — либо `explanation.summary` (если `explanation` — словарь), либо строковое представление всего `explanation` | `:100-111, :218` |
| `description_explanation_summary` | `description + "\n" + explanation_summary` (только непустые части) | `:219-221` |
| `description_task_description_summary` | `description + "\n" + (task_description_summary or task_description)` | `:222-224` |

Каждый непустой документ кладётся с `meta_json = {"card_id", "snippet": description или первая строка, "document_kind"}` (`:232-243`). Пустые (после `_stringify`) — не создаются вовсе, число документов на карточку варьируется от 0 до 6.

Покрытие тестами неровное: `grep -rln "derive_memory_card_search_documents" api/tests` находит только `test_search_document_agent_skill.py`, и там карточковая деривация используется лишь как шпион в тесте роутинга (`:193-205`), без проверки количества видов. `DOCUMENT_KIND_FULL_CARD` фигурирует в `test_unified_search.py` и `test_search_document_agent_skill.py:156,160` (только дефолты BM25/vector). **`DOCUMENT_KIND_DESCRIPTION_EXPLANATION_SUMMARY` и `DOCUMENT_KIND_DESCRIPTION_TASK_DESCRIPTION_SUMMARY` не встречаются вообще нигде в `api/tests`** — два из шести видов не имеют ни одного теста, который бы их производил или искал по ним.

### `agent_skill` — 4 вида (`derive_agent_skill_search_documents:141`, `raw_docs` на `:170-179`)

| `document_kind` | Содержимое | Адрес |
|---|---|---|
| `skill_description` | `name + "\n" + description` | `:171-173` |
| `skill_instructions` | сырое тело `instructions` (SKILL.md) | `:174` |
| `skill_full` | `name + description + instructions` | `:175-177` |
| `skill_allowed_tools` | `allowed_tools` через `_stringify` (список → CSV) | `:178` |

`card_id` для skill-документов — `name` или, если пусто, `uri` (`:168`, комментарий `:156-159` объясняет двойное назначение колонки). Покрыто явно: `test_search_document_agent_skill.py::TestDeriveAgentSkillDocs::test_full_skill_produces_four_doc_kinds` (`:56`).

### Дефолты по типу для поиска

`BM25_DEFAULT_DOCUMENT_KINDS` / `VECTOR_DEFAULT_DOCUMENT_KINDS` (`:54-62`): BM25 → `full_card` (memory_card) / `skill_full` (agent_skill); vector → `full_card` (memory_card) / `skill_instructions` (agent_skill) — для `agent_skill` BM25 и vector по умолчанию бьют по разным документам (полный текст против одних инструкций), а для `memory_card` — по одному и тому же `full_card`.

## Дедупликация

Ручка: `GET /v1/{entity_type}/duplicates` (`routers/dedup.py:26-30`), смонтирована с `prefix="/v1"` **до** типовых роутеров сущностей в `main.py:69-73` — комментарий поясняет: иначе литерал `"duplicates"` парсился бы как UUID `entity_id` в generic-роуте. Порог `threshold: float = Query(0.95, ge=0.5, le=1.0)` (`dedup.py:37-46`) — дефолт `0.95`, диапазон `[0.5, 1.0]` ограничен на уровне FastAPI `Query`.

Алгоритм — `EntityService.find_duplicate_pairs` (`entity_service.py:1230-1346`): CTE `channel_versions` резолвит канал → одну версию на сущность с её эмбеддингом, затем self-join `a.entity_id < b.entity_id` (канонизация пары + исключение самосравнения) с фильтром `(1 - (a.embedding <=> b.embedding)) >= :threshold`, сортировка по убыванию схожести. При `enable_vector_search=False` метод возвращает `None` (`entity_service.py:1256-1257`), роутер превращает в `503` (`dedup.py:93-101`). При отсутствии эмбеддингов — структурированный `{"pairs": []}`, `200` (докстринг `:1249-1254` явно разводит эти два случая).

Тест: `test_semantic_dedup.py`, **294 строки**, три слоя по докстрингу файла (`:1-11`): сервис (feature-flag, SQL-параметры, форма пары, namespace-фильтр), роутер (валидация `entity_type`, `503`, проброс query-параметров), OpenAPI (регистрация эндпоинта/схемы ответа). В CI (`ci.yml`) этот файл не гоняется — см. раздел «Покрытие тестами».

---

## Версионирование и lineage

- **`record_run`** (`entity_service.py:574-606`) — сигнатура `async def record_run(self, entity_id: uuid.UUID, run_id: str | None = None)`. Докстринг (`:580-582`) говорит: «`run_id` is accepted for forthcoming idempotency (a Redis LRU of recent run_ids will dedupe accidental double-bumps) but is currently a documentation slot only». Тело метода (`:587-606`) параметр `run_id` не читает вообще — бампает `run_count`, ставит `last_run_at`, коммитит, публикует событие. `docs/CARE_INTEGRATION.md:168-169` описывает механизм как существующий факт: «Idempotency hook: optional `run_id` body field deduplicates double-recordings via an in-memory LRU» — без пометки «план». Не покрыто ни одним тестом на сам параметр (тестируется лишь косвенно эффект `record_run` без `run_id`, см. «Покрытие тестами»).
- **`find_versions_beating`** (`entity_service.py:957-1064`) — резолвит `baseline_channel` (дефолт `"stable"`) → версию → `_extract_objective_value` (`:919-955`, `fitness_score` с фоллбэком на legacy `fitness`, либо `evolution_meta.objectives[<objective>]`). Полный скан всех версий сущности (`:1027-1029`). Строгое `>` (не `>=`) — тай остаётся у инкумбента (`:982-984`, `:1037`). `sort_dir` (`:964`) участвует как `reverse = sort_dir == "desc"` (`:1053`) — **без `.lower()`**, в отличие от `list_entities` (`:772`: `descending = sort_dir.lower() == "desc"`). Разбор достижимости — см. таблицу в разделе «Заглушки, дыры и мины».
- **`diff_versions`** (`entity_service.py:1066-1080`) — докстринг: «Compute JSON Merge Patch between two versions» (`:1069`). Реализация: `jsonpatch.make_patch(from_ver.content_json, to_ver.content_json)` — библиотека **RFC 6902** (JSON Patch, операции `add/remove/replace/move/copy/test`), не **RFC 7396** (JSON Merge Patch). Независимо подтверждается в `diff_html.py:3` («The JSON-format response carries the raw RFC-6902 patch operations») и константой `_KNOWN_OPS = ("add", "remove", "replace", "move", "copy", "test")` (`diff_html.py:20`).
- **Lineage BFS** — `get_lineage` (`entity_service.py:829-917`), не `_resolve_version`. Обход `entity_versions.parents` слой за слоем (`:868-887`), `max_depth: int = 10` по умолчанию (`:835`), де-дупликация по `version_id` в `visited` (`:865`, `:882`) — мультиродительский кроссовер попадает в выдачу один раз. `max_depth_reached` (`:890`) — `True`, если после цикла во `frontier` остались непосещённые узлы (упёрлись в потолок глубины, не в отсутствие родителей). Порядок выдачи — по `depth` возрастающе, внутри слоя по `version_number` убывающе (`:894-897`). Ручка — только `GET /v1/chains/{id}/lineage` (`chains.py:440`); у `agent_skill`, `memory_card`, `step`, `agent` эквивалента нет (см. «Заглушки, дыры и мины»).
- **`_resolve_version`** (`entity_service.py:1171-1193`) — мёртвый код: `grep -rn "_resolve_version\b" api/app api/tests`, исключая `_resolve_version_metadata` и `_get_metadata_source_version`, даёт единственное совпадение — само определение метода. Ни один вызывающий код, ни один тест.
- **`_resolve_version_metadata`** (`entity_service.py:123-144`, `@staticmethod`) — принимает `source_version: EntityVersion | None`. Строка 133: `source_meta = source_version.meta_json or {}` — при `source_version=None` это `AttributeError: 'NoneType' object has no attribute 'meta_json'`, без guard. `grep -rn "_resolve_version_metadata" api/tests` — ноль совпадений, ветка `None` не проверяется нигде.

---

## Аутентификация и изоляция

### API-ключи (`api_key_service.py`)

Опаковый токен — `secrets.token_urlsafe(32)` (`:38-40`, ≈43 символа). Хранится только `SHA-256`-хеш (`_hash_key`, `:29-35`), plaintext возвращается ровно один раз в `IssuedKey` (`:43-59`). `verify_key` (`:106-125`) режектит по `revoked_at IS NOT NULL` и `expires_at <= now()`. Управление ключами — только через CLI (`api/app/create_key.py`); HTTP-ручки для issue/list/revoke не существует (`find api/app/routers -iname "*key*"` — пусто). `ApiKeyService.revoke_key` и `list_keys` (`:127-157`) вызываются только из тестов (`grep -rn "\.revoke_key(\|\.list_keys(" api/app` вне `tests/` — ноль совпадений); `list_keys` не вызывается вообще нигде, включая тесты (`grep -rn "list_keys" api/tests` — ноль). Revocation в проде не воспроизводима иначе как прямым доступом к БД.

### Скоупы (`auth.py:66-97`)

Шесть канонических скоупов: `read:any`, `write:any`, `delete:any`, `clear:all`, `admin:keys`, `evolve`. Роли: `ROLE_READER = {read:any}`, `ROLE_EDITOR = {read:any, write:any}`, `ROLE_ADMIN = ALL_SCOPES`.

**Реально гейтится только один скоуп во всём приложении**: `grep -rn "require_scope(" api/app/routers` даёт единственный вызов — `routers/entities.py:218`, `auth.require_scope(SCOPE_CLEAR_ALL)` перед `POST /maintenance/clear-all` (маршрут объявлен `entities.py:181`, `Depends(require_api_key)` рядом на `:185`). `grep -rn "has_scope(\|SCOPE_WRITE_ANY\|SCOPE_DELETE_ANY\|SCOPE_ADMIN_KEYS\|SCOPE_EVOLVE" api/app`, исключая `auth.py`, — **ноль совпадений**: `write:any`, `delete:any`, `admin:keys`, `evolve` объявлены, задокументированы, включены в роли, но ни одна ручка их не проверяет.

Это не просто «зарезервировано на будущее» (как честно написано для `evolve` в `auth.py:56-57`) — это ещё и **прямое расхождение с докстрингом `default_namespace_for`**: комментарий на `auth.py:154-156` гласит «Authenticated caller with an explicit `meta_namespace` → respected verbatim. Caller is deliberately writing to a shared workspace; **the service layer enforces scope checks**». Сама функция (`auth.py:143-168`) никакой скоуп не проверяет — просто пропускает `meta_namespace` как есть, если он не `None`; а «service layer» (роутеры `agent_skills.py`, `agents.py`, `bulk.py`, `chains.py`, `memory_cards.py` — все пять зовут `default_namespace_for(...)` без окружающей проверки `has_scope`, на примере `memory_cards.py:36`) тоже ничего не проверяет. **Практически: любой аутентифицированный держатель ключа, включая `ROLE_READER` без единого write-скоупа, может писать в чужой namespace, просто указав его явно в теле запроса** — `write:any` эту возможность нигде не разрешает и не отбирает. `test_auth_scopes.py` (138 строк) тестирует только механику `AuthContext.has_scope`/`require_scope` на синтетических контекстах (`:99-138`) — ни один тест не бьёт по реальной ручке, чтобы проверить, что `write:any` действительно требуется для кросс-namespace записи, потому что такой проверки нет.

**Дыра шире, чем сами скоупы**: `Depends(require_api_key)` в принципе навешан только на `POST`-create и `GET`-list каждого типового роутера — `agent_skills.py:112,211`, `agents.py:50,127`, `chains.py:148,231`, `memory_cards.py:24,82`, `steps.py:65` (у `steps.py` даже create, `:16-20`, без auth вообще). Ни один `PUT`/`PATCH`/`DELETE`/`favourite`/`run-recorded`/`lineage`/`versions/beating` не принимает `auth: AuthContext = Depends(require_api_key)` — по всем 13 файлам роутеров грепом это ни разу не встречается рядом с этими методами. То же для `entities.py` (generic `PUT`/`DELETE` — без auth, только `/maintenance/clear-all` требует ключ) и всего `versions.py` (`revert`/`pin`/`promote`/`diff` — без auth вообще). Итог: даже при `AUTH_REQUIRED=true` (`config.py:32`) обновление, патч, удаление, favourite-тоггл, запись прогона, ревёрт/пин/промоут и просмотр lineage/diff **не требуют ключа вовсе** — под защитой только создание и листинг. Скоупы `write:any`/`delete:any` в таком контуре физически не могут ничего проверять на мутациях: сами ручки-мутаторы не проверяют даже наличие ключа, не то что скоуп внутри него.

### OIDC (`oidc.py`)

`settings.oidc_enabled: bool = False` (`config.py:49`) — по умолчанию выключен, `get_oidc_verifier()` тогда возвращает `None` (`:215-216`) без похода в сеть. Верификация: JWKS с `JWKSCache` (TTL `oidc_jwks_cache_ttl_seconds=600`, `:55`) — при неудачном фетче отдаёт последний хороший кэш, если он есть (`:80-86`, stale-but-usable), иначе кидает `OIDCError`. Ротация ключа: `JoseError` на первой попытке → принудительный `force_refresh=True` и повтор ровно один раз (`:143-155`). Клеймы: `iss`/`exp` всегда `essential`, `aud` опционально (`:157-162`), `leeway_seconds=30` (`config.py:57`) на скошенные часы. `_normalise_scopes` (`:182-200`) принимает и пробельно-разделённую строку (OAuth2 `scope`), и список строк (Auth0/Keycloak `scopes`); любая другая форма (включая `None`) → пустой `frozenset`, без ошибки.

### Dual-mode (`auth.py:213-317`, `require_api_key`)

Bearer (`Authorization:`) проверяется первым, если присутствует — побеждает над `X-API-Key`, даже если оба заголовка есть (`:225-226`, докстринг явно). Присланный Bearer при `oidc_enabled=False` → `401` с явным текстом «Bearer token received but OIDC is disabled» (`:251-261`), не тихий фоллбэк на anonymous. При отсутствии обоих заголовков: `auth_required=True` → `401`; `auth_required=False` (дефолт, `config.py:32`) → анонимный `AuthContext(key_id="", owner="anonymous", scopes=frozenset())` (`_anonymous_context`, `:134-140`). Невалидный/отозванный `X-API-Key` — всегда `401`, даже в opt-in режиме (`:290-297`): протухший ключ не может тихо откатиться до анонимного доступа.

### Namespace-изоляция чтения

`default_read_namespace_for` (`:171-210`): анонимный проходит query-namespace как есть; аутентифицированный с явным `?namespace=` — уважается; без явного namespace и с `read:any` — `None` (видит все namespace); без `read:any` — принудительно `auth.owner`. Это единственное место во всём модуле аутентификации, где скоуп (`read:any`) реально на что-то влияет.

---

## Инфраструктура

### SSE и события

Паблишер — `events/publisher.py`, Redis pub/sub, канал `"memory:events"` (`:10`). `publish_entity_event` (`:29-56`) не оборачивает `await r.publish(...)` (`:56`) в try/except — если Redis недоступен, вызов кидает исключение сквозь весь стек (`record_run`, `pin_channel`, `promote`, `update_metadata` и т. д. делают `await self.db.commit()` **до** `publish_entity_event(...)`), то есть запись в Postgres уже закоммичена, а клиент получает `500` из-за недоступного Redis — не может отличить «запись не прошла» от «запись прошла, но нотификация не долетела».

Читающая сторона — `routers/events.py`. Backpressure — измерение лага между `event["timestamp"]` и моментом обработки в генераторе SSE: `_compute_lag_action` (`:18-65`, чистая функция без I/O), три действия: `forward` (в пределах `sse_warn_lag_seconds=10.0`, `config.py:65`), `warn` (между warn и `sse_drop_lag_seconds=60.0`, `config.py:66` — инжектит `lag_warning`, но всё равно форвардит оригинал), `drop` (сверх порога — шлёт финальный `lag_warning` и закрывает соединение, `:159-162`). Серверные фильтры (`_event_passes_filters`, `:68-96`): `entity_type`/`entity_id`/`namespace`/`event_type` — точное совпадение (AND), `tags` — пересечение множеств (OR внутри себя). Вебхуки — заглушка: `POST /webhooks` и `DELETE /webhooks/{id}` оба всегда `501` (`:175-184`).

### Prometheus-метрики (`metrics.py`)

Отдельный `CollectorRegistry` (`:50`), три серии: `gigaevo_memory_http_requests_total` (counter, `method`/`path_template`/`status`), `gigaevo_memory_http_request_duration_seconds` (histogram, бакеты `0.005…10.0` секунд, `:55-58`), `gigaevo_memory_entities` (gauge по `entity_type`). `path_template` берётся из `request.scope["route"].path` (`:88-101`), не из сырого URL, несматченные пути схлопываются в `"unmatched"`. Мидлварь (`:109-143`) пропускает собственный путь `/metrics` (`:119-120`), пессимистично инициализирует `status = 500` (`:123`) и перезаписывает на успехе — при исключении в хендлере счётчик всё равно фиксирует `500` через `finally` (`:132-143`). `refresh_entity_counts` (`:151-176`) — best-effort: при `SQLAlchemyError` тихо оставляет старые значения гейджа (`:167-170`, коммент отсылает к алерту `absent_over_time` на стороне Prometheus). `entities_gauge.clear()` перед репопуляцией (`:174`) — тип, обнулённый до нуля сущностей, реально исчезает из экспорта.

### Бэкапы

`deploy/scripts/backup.sh` — `pg_dump` внутри контейнера `postgres` через `docker compose exec`, пайп в `gzip` на хосте, файл `gigaevo-memory-<UTC timestamp>.sql.gz` (`:60-61`); опциональная заливка в S3 при `S3_BUCKET` (`:89-96`). `set -euo pipefail` (`:30`) действует и на `eval`-нутый пайплайн дампа (`:64-70`, `run()`), так что упавший `pg_dump` действительно валит скрипт, а не тихо пишет пустой `.gz`. Покрыт `test_backup_script.py` (196 строк, 16 тестов).

### Миграции с round-trip-гейтом

`.github/workflows/migration-safety.yml` — триггерится на изменения `api/app/db/migrations/**`, `models.py`, `alembic.ini`. Шаги: статический `pytest tests/test_migration_chain.py` (быстрый пре-флайт без БД, `:55-59`) → `alembic upgrade head` на чистой БД (pgvector/pg15) → `alembic downgrade -1` → `alembic upgrade head` повторно (проверка идемпотентности) → `alembic current` для лога. `Makefile`'s `migrate-check` зеркалит те же пять шагов локально («matches CI gate» — прямая цитата из комментария Makefile).

---

## Документация

`docs/` — 4 файла: `AGENT_SKILL_ENTITY.md` (12.4K), `CARE_INTEGRATION.md` (12.5K), `CHAIN_CONTENT_CONVENTIONS.md` (6.8K), `EVOLUTION_META.md` (11.2K).

### `docs/CARE_INTEGRATION.md`

Зонтичный контракт CARE↔Memory: 5 типов сущностей и их префиксы (`:36-42`), правило записи namespace (3 кейса, `:58-65`) и чтения (4 кейса, `:67-75`) через `default_namespace_for`/`default_read_namespace_for`, dual-mode auth (`:87-98`), 6 скоупов (`:102-109`), 3 role-бандла (`:120-122`), каналы `latest`/`stable`/`evolved` (`:132-136`), 5 CARE-библиотечных колонок из миграции 003 (`:152-158`), 8 типов SSE-событий (`:247-256`).

### `docs/AGENT_SKILL_ENTITY.md`

Схема контента (11 полей, 4 обязательных: `name`/`description`/`uri`/`sha256`, `:31-43`), 4 формы `uri` (`:47-54`), 8 REST-маршрутов включая `PATCH` и `/favourite`/`/run-recorded` (`:63-72`), 4 вида поисковых документов (`:110-115`).

### `docs/EVOLUTION_META.md`

Две концентрические схемы `evolution_meta` (`:21-51`), 5 правил auto-promotion канала `evolved` с примером на 5 поколениях (`:112-154`), `GET /v1/chains/{id}/lineage`, BFS, `max_depth` 1–100 (`:156-224`).

### `docs/CHAIN_CONTENT_CONVENTIONS.md`

Конвенция для `content["metadata"]` внутри `chain` (сервер не валидирует, content — непрозрачный JSON). `Entity.display_name` (мутабельная БД-колонка) vs `metadata.display_name` (встроенная копия) — на чтении авторитетна БД-колонка (`:82-95`), CARE пишет обе. `merge_into_content()` сохраняет чужие ключи рядом с CARE-блоком (`:133-134`). Внутренних противоречий не найдено.

### Расхождения дока с кодом и дока с самим собой

- **Пагинация**: `CARE_INTEGRATION.md:178` (таблица List query knobs) заявляет дефолт `sort_by=last_run_at` (и `sort_dir=desc`, `:187-188`), но `:198` (раздел Pagination) говорит «Only valid with the default sort (`created_at asc`)» — то есть под собственным же дефолтом CARE курсорная пагинация никогда не работает, сервер молча деградирует до offset. Подтверждается кодом: `TODO.md:370-373` (ветка `who-cares`) прямо признаёт: «Cursor pagination only applies when sort matches its encoding (`created_at asc`); other sorts silently ignore the cursor and use offset».
- **Имена tool-фильтров**: `AGENT_SKILL_ENTITY.md:91-92` (HTTP-таблица) называет параметры `requires_tool`/`excludes_tool` (единственное число), а `:170-172` (пример кода SDK) — `requires_tools=[...]`/`excludes_tools=[...]` (множественное). Это не опечатка: `TODO.md:1637-1641` подтверждает намеренность — итерация #22 сознательно называет клиентский метод `list_agent_skills(requires_tools=..., excludes_tools=...)` (плюрал), сериализующий их в повторяющиеся query-параметры `requires_tool`/`excludes_tool` (сингулар) на проводе; но `AGENT_SKILL_ENTITY.md` эту асимметрию нигде не поясняет сноской.
- **Заявления про OpenAPI**: докстринг `AgentSkillContent` (`requests.py:254`) гласит «gives the OpenAPI surface a typed component CARE/MAGE clients can validate against locally», и `AGENT_SKILL_ENTITY.md:261-262` вторит: «Memory does not validate `content` against `AgentSkillContent` server-side… The model is exposed via OpenAPI so clients can validate locally» — вторая половина утверждения фактически неверна: `MemoryCardContent`/`AgentSkillContent`/`CareChainMetadata`/`ContextFileRef` в `openapi.yaml` отсутствуют целиком (`grep -n` — 0 совпадений на все четыре имени), потому что маршруты типизируют `content: dict[str, Any]`, а не эти модели, и FastAPI включает в спеку только схемы, реально фигурирующие в сигнатурах эндпоинтов. Тест `test_agent_skill_content.py:120-137` (класс `TestAgentSkillContentOpenAPI`, буквально «Schema reaches the OpenAPI surface») эту claim не проверяет — вызывает только `AgentSkillContent.model_json_schema()` (чистый Pydantic-экспорт), ни разу не `app.openapi()`. Для контраста, `EvolutionMeta` действительно в `openapi.yaml` (строки 332, 722, 884, 899, 914, 989) и `test_evolution_meta.py:155` реально дёргает `app.openapi()[...]["EvolutionMeta"]` — потому что это поле реально типизировано в `EntityCreateRequest`/`EntityUpdateRequest`/`BulkSaveItem`.
- **LRU в `record_run`**: `docs/CARE_INTEGRATION.md:168-169` описывает in-memory LRU дедупликацию `run_id` как существующий факт («Idempotency hook… deduplicates double-recordings via an in-memory LRU»); код (`entity_service.py:574-606`) параметр `run_id` принимает и не читает вовсе. При этом внутренний `TODO.md:292` (ветка `who-cares`) сам честно называет это «future-idempotency hook… deferred — see P1» — то есть внутренний плановый документ характеризует функциональность правильно (план, не факт), а вынесенный наружу `docs/CARE_INTEGRATION.md` — нет.

---

## Покрытие тестами

### Числа (посчитаны, не на глаз)

| Что считалось | Команда | Значение |
|---|---|---|
| Тестовых файлов в `api/tests` | `find api/tests -maxdepth 1 -name "test_*.py" \| wc -l` | **60** |
| Тестовых функций, только синхронные (`^def test_\|^    def test_`) | сумма по всем файлам | **670** |
| Тестовых функций, sync+async (`^def test_\|^    def test_\|^async def test_\|^    async def test_`) | сумма по всем файлам | **838** |
| Строк Python во всём `api/app` | `find api/app -name '*.py' \| xargs wc -l` | **10177** |
| Строк Python во всём репозитории (`api/app`+`api/tests`+`web_ui`+прочее) | `find . -name '*.py' \| xargs wc -l` | **29894** |

«~30k строк Python, 838 тестовых функций» — верно **для всего репозитория** (29894 ≈ 30k), не для `api/app` в одиночку (там 10177). 838 — точное число тестовых функций в `api/tests`, но только если считать `async def test_` вместе с `def test_`; буквальный `grep`-паттерн без учёта `async def` ловит только 670 — недосчитывает ровно 168 асинхронных тестов, что объяснимо: почти все сервисные/роутерные тесты в этом кодбейзе `async` (`pytest-asyncio`, `asyncio_mode = auto` в `api/pytest.ini:2`). Если добавить `web_ui/tests` (2 файла, 41 тестовая функция), сумма растёт до 879 — 838 относится именно к `api/tests`.

### CI: 7 селекторов, **6** уникальных файлов из 60

`.github/workflows/ci.yml`, job `lint-and-test`, гоняет `python -m pytest` с семью явными таргетами (`working-directory: api`): `tests/test_entity_service.py`, `tests/test_embedding_service.py`, `tests/test_vector_utils.py`, `tests/test_health.py::TestHealthUnit`, `tests/test_embeddings.py::TestEmbeddingsRequestResponse`, `tests/test_embeddings.py::TestEmbeddingsEndpointUnit`, `tests/test_events.py::TestEventPublisherUnit`. Это 7 строк-селекторов, но 6 уникальных файлов (`test_embeddings.py` встречается дважды — двумя разными классами внутри одного файла). `Makefile`'s `test-api-unit` (та же команда, дословно то же перечисление) подтверждает, что это не опечатка CI, а сознательно узкий «офлайн-безопасный» срез — докстринг в `ci.yml` прямо говорит: «Docker-backed integration tests… stay out of this gate; the alembic round-trip is covered by migration-safety.yml». Остальные 54 файла из 60 (включая `test_semantic_dedup.py`, весь `search_strategies`, `auth.py`/`oidc.py`, SSE, метрики) гоняются только вручную/через `make test-api-all` (Docker-профиль). `migration-safety.yml` отдельно гоняет `tests/test_migration_chain.py` как статический пре-флайт — этот файл не входит в список CI выше, но покрыт другим workflow'ом.

### Таблица покрытия по возможностям

| Возможность | Покрыта тестом | Файл(ы) | В CI (`ci.yml`)? |
|---|---|---|---|
| BM25/vector/hybrid стратегии, `SearchStrategy` | да | `test_unified_search.py` (16), `test_indexed_document_search_strategies.py` (14) | нет |
| Vector search API (эндпоинты) | да | `test_api_vector_search.py` (7) | нет |
| Реранкер (identity + реестр) | да | `test_reranker.py` (16) | нет |
| Embedding-сервис / бэкенды | да | `test_embedding_service.py` (16) | **да** |
| `/v1/embeddings` эндпоинт | да | `test_embeddings.py` (11) | **да** (частично, 2 класса) |
| `vector_utils` (валидация, сериализация) | да | `test_vector_utils.py` (20) | **да** |
| Инструмент-фильтры agent_skill (`requires_tool`/`excludes_tool`) | да | `test_allowed_tools_filter.py` (17), `test_search_tool_filters.py` (6) | нет |
| Namespace-скоуп поиска | да | `test_search_auth_namespacing.py` (14) | нет |
| Дедупликация | да | `test_semantic_dedup.py` (14, 294 строки) | нет |
| `diff_versions` / HTML-рендер | да | `test_diff_html.py` (31), `test_api_versions.py` (9) | нет |
| Lineage BFS | да | `test_lineage_endpoint.py` (11) | нет |
| `find_versions_beating` | да | `test_differential_channel.py` (24) | нет |
| `record_run`/`run_count`/`last_run_at` | да (косвенно, через роутеры) | `test_library_mutations.py`, `test_library_mutation_events.py`, `test_events_firehose.py`, `test_sse_backpressure.py`, `test_care_integration_doc.py`, `test_agent_skills_router_library.py`, `test_chains_router_library.py` | нет |
| `run_id`-дедупликация в `record_run` | **нет** — параметр принят, не используется | — | — |
| `_resolve_version_metadata` c `source_version=None` | **нет** | — | — |
| `_resolve_version` (мёртвый метод) | нет вызовов, тестировать нечего | — | — |
| API-ключи: создание/верификация/revoke | да (сервис) | `test_api_key_auth.py` (29), `test_create_key_cli.py` (26) | нет |
| `ApiKeyService.list_keys` | **нет, ни разу** | — | — |
| Dual-mode auth (`auth_required` вкл/выкл) | да | `test_auth_dual_mode.py` (12) | нет |
| Скоупы (`has_scope`/`require_scope`) на синтетических контекстах | да (мех., не e2e) | `test_auth_scopes.py` (14) | нет |
| `write:any`/`delete:any`/`admin:keys`/`evolve` реально гейтят ручку | **нет** — эти скоупы нигде не проверяются в роутерах | — | — |
| OIDC верификация (JWKS, ротация, клеймы) | да | `test_oidc.py` (35, 485 строк) | нет |
| CORS | да | `test_cors.py` (9) | нет |
| SSE поток + backpressure (`_compute_lag_action`) | да | `test_sse_backpressure.py` (12), `test_events.py` (10), `test_events_firehose.py` (14) | частично (`test_events.py::TestEventPublisherUnit`) |
| Prometheus-метрики | да | `test_metrics.py` (17) | нет |
| Health / health-enrichment | да | `test_health.py` (8), `test_health_enrichment.py` (7) | частично (`TestHealthUnit`) |
| Backup-скрипт | да | `test_backup_script.py` (16) | нет |
| Миграционная цепочка (статически) | да | `test_migration_chain.py` (8) | нет (свой workflow `migration-safety.yml`) |
| Entity-service базовые операции (etag, курсор, типы) | да | `test_entity_service.py` (19) | **да** |
| Doc-drift гварды (README/CARE_INTEGRATION/evolution_meta против кода) | да | `test_readme_architecture.py`, `test_care_integration_doc.py` (299 строк), `test_evolution_meta_doc.py`, `test_agent_skill_entity_doc.py`, `test_care_chain_metadata.py` | нет |
| Легаси-импорты `gigaevo_memory` запрещены (AST-скан) | да | `test_no_legacy_gigaevo_memory_imports.py` | нет |

---

## Заглушки, дыры и мины

| Что | Где | Комментарий |
|---|---|---|
| Вебхуки | `api/app/routers/events.py:176`, `:182` | `POST /v1/webhooks` и `DELETE /v1/webhooks/{id}` возвращают `501`. Присутствуют в `openapi.yaml` — легко принять за рабочие. **[ревизия 2026-07-29]**: в `openapi.yaml:4544-4583` единственный документированный ответ подписан `'501': description: Successful Response` — автоген FastAPI помечает объявленный код ярлыком «Successful Response» независимо от смысла кода, то есть спека буквально называет отказ «успешным». В коде это при этом честная заглушка (`raise HTTPException(status_code=501, ...)`), не тихий fail-open. |
| Реранкеры | `search_strategies/reranker.py` | Зарегистрирован только `IdentityReranker` (no-op). Cross-encoder не поставляется. Инфраструктура есть, реализаций нет. Покрыто `test_reranker.py` (271 строка, 16 тестов) — все против identity + регистрационной механики, ни одного реального реранкера в поставке. |
| `run_id` в `record_run` | `entity_service.py:574` | Параметр принимается, документируется как «will be used to dedupe accidental double-recordings via a short-lived in-memory LRU», но не используется. `docs/CARE_INTEGRATION.md:168` описывает LRU как существующий — это неточность документации. **[ревизия 2026-07-29]**: полный диапазон метода `:574-606`, докстринг — `:580-582`, доклад в CARE_INTEGRATION.md — `:168-169`. Внутренний `TODO.md:292` (ветка `who-cares`) сам характеризует это как «deferred — see P1», то есть внутренний план верен, наружу вышла неточность. |
| `_resolve_version` | `entity_service.py:1171` | Мёртвый код: дублирует `get_entity(fallback=True)`, вызывающих в репозитории нет. **[ревизия 2026-07-29]**: точный диапазон `:1171-1193`; `grep -rn "_resolve_version\b" api/app api/tests` (с исключением `_resolve_version_metadata`/`_get_metadata_source_version`) даёт единственное совпадение — само определение. Ни одного теста. |
| Типизированные алиасы версий | `versions.py:206-281` | 8 роутов (`/v1/steps/{id}/versions` и т.д.) объявлены после generic `/{entity_type}/{id}/versions` в том же роутере → недостижимы. Существуют только чтобы дать OpenAPI типизированные операции. **[ревизия 2026-07-29]**: механизм — `entity_type="steps"` тривиально подходит под шаблон `/{entity_type}/{entity_id}/versions`, поэтому маршрут матчится первым (FastAPI/Starlette матчит по порядку регистрации); из четырёх типов, у которых в файле есть typed-алиасы (steps/chains/agents/memory_cards), не работает **ни один** — работает только generic-путь; у `agent_skill` алиаса нет вовсе. `openapi.yaml` документирует все восемь как отдельные рабочие пути без пометки о недостижимости. |
| `memory_cards` и `steps` — второсортные | `memory_cards.py`, `steps.py` | У них нет `PATCH`, `/favourite`, `/run-recorded` (в отличие от chains/agents/agent_skills). Для Ideas Lake, где карточка идеи = `memory_card`, это прямой пробел. **[ревизия 2026-07-29]**: точный список маршрутов — `memory_cards.py`: `POST ""` (`:21`), `GET ""` (`:70`), `GET "/{id}"` (`:128`), `PUT "/{id}"` (`:163`), `DELETE "/{id}"` (`:217`); `steps.py` — тот же набор из пяти (`:16,:57,:114,:149,:203`). `agents.py`/`agent_skills.py` добавляют к тем же пяти PATCH+favourite+run-recorded (`agents.py:243,278,300`; `agent_skills.py:337,371,393`); `chains.py` — ещё и `/lineage` (`:440`) и `/versions/beating` (`:478`), которых нет вообще ни у кого другого. |
| `agent_skills` не имеет `/lineage` | — | Только у `chains` (`docs/EVOLUTION_META.md:221-223`: «Same shape can be added to other typed routers if MAGE/CARE start evolving agents, agent_skills, or memory_cards»). |
| `routers/__init__.py` рассинхронизирован | `api/app/routers/__init__.py:3-16` | Экспортирует 10 модулей из 13: `agent_skills`, `bulk`, `dedup` отсутствуют. `from .routers import *` молча потеряет три роутера. **[ревизия 2026-07-29]**: на практике это не функциональная дыра — `main.py:12-26` делает прямой `from .routers import (agent_skills, agents, bulk, chains, dedup, embeddings, entities, events, health, memory_cards, steps, unified_search, versions)`, импорт подмодуля не зависит от `__all__`; форма `from .routers import *` в репозитории нигде не встречается (проверено грепом). Несовпадение 10 vs 13 — косметическая/документационная нестыковка, не риск для маршрутизации. |
| `content` нигде не валидируется | `docs/AGENT_SKILL_ENTITY.md:262-264` | «The server will accept a malformed body — it's the caller's responsibility». `evolution_meta` тоже не валидируется на уровне хранения (`docs/EVOLUTION_META.md:257-259`). Судя по формулировке обоих мест дока («read-leniently / write-strictly»), это осознанная архитектурная позиция, а не недосмотр. |
| `_resolve_version_metadata` без None-guard | `entity_service.py:123-133` | Аннотация `source_version: EntityVersion \| None`, но на строке 133 идёт `source_version.meta_json or {}` без проверки → `AttributeError` при нерезолвящемся источнике. **[ревизия 2026-07-29]**: `grep -rn "_resolve_version_metadata" api/tests` — ноль совпадений, метод не покрыт ни одним тестом, ветка `None` не проверяется нигде. |
| `sort_dir` без `.lower()` в `find_versions_beating` | `entity_service.py:1053` | Точное сравнение `== "desc"`; `"DESC"` молча даст восходящую сортировку. В `list_entities:772` — с `.lower()`. Несогласованность. **[ревизия 2026-07-29]**: на уровне HTTP это недостижимо — оба роутера (`routers/chains.py:210-214,498-502`) валидируют `sort_dir` регуляркой `^(asc|desc)$`, пропускающей только строчные значения, так что `.lower()` в `list_entities` сейчас тоже мёртвый защитный код, а не активно нужный. Несогласованность реальна только для прямых вызовов `EntityService` в обход HTTP-роутеров. |
| Кэш эмбеддингов неограничен | `embedding_service.py:245` | `self._cache: dict[str, list[float]]` без eviction. Плюс O(n²) в `embed_batch:352` (`uncached_indices.index(idx)` в цикле). |
| `diff_versions` докстринг врёт | `entity_service.py:1069` | Говорит «JSON Merge Patch», выдаёт RFC 6902 JSON Patch. **[ревизия 2026-07-29]**: независимо подтверждено в `diff_html.py:3` («raw RFC-6902 patch operations») и константой `_KNOWN_OPS = ("add", "remove", "replace", "move", "copy", "test")` на `diff_html.py:20` — словарь операций JSON Patch, не Merge Patch. |
| Порядок роутеров — единственная защита | `api/app/main.py:70-100` | Три комментария об одной проблеме. Любой новый 2-сегментный `/v1/*`-путь, смонтированный после строки 97, будет затенён generic-роутером. **[ревизия 2026-07-29] — расхождение адресов между источниками**: по одному разбору те же три места — `main.py:69-72` (dedup), `:88-89` (unified_search/embeddings), `:94` (events), `:97` (entities), `:100` (versions); по другому — `main.py:69-73` (dedup), `:85-88` (unified_search/embeddings), `:91-94` (events). Оба подтверждают три отдельных комментария об одном и том же риске, расхождение только в точных границах диапазона строк. |
| Web UI: вкладки Steps и Agents закомментированы | `web_ui/app/main.py:86-89` | Живые вкладки: Chains, Agent Skills, Memory Cards, Search, Showcase, Maintenance. |
| `openapi.yaml` частично устарел | `openapi.yaml` | `MemoryCardContent`, `AgentSkillContent`, `CareChainMetadata` в компонентах отсутствуют (роутеры принимают `dict[str, Any]`); `connected_ideas` в спеке нет вовсе. `EvolutionMeta` — есть. **[ревизия 2026-07-29]**: причина структурная, не «забыли обновить» — маршруты типизируют `content` как `dict[str, Any]`, поэтому FastAPI никогда не включает эти 4 модели в спеку (сравни `EvolutionMeta`, которая реально типизирует поле `evolution_meta` и потому в спеке есть — строки 332,722,884,899,914,989). Тест `test_agent_skill_content.py:120-137` (класс `TestAgentSkillContentOpenAPI`) заявляет в имени «Schema reaches the OpenAPI surface», но реально вызывает только `.model_json_schema()`, ни разу `app.openapi()` — тест не проверяет то, что заявляет. |
| CI-покрытие узкое | `.github/workflows/ci.yml` | 7 из 60 файлов. Регрессия в auth/search/SSE/dedup не будет поймана публичным гейтом. **[ревизия 2026-07-29] — число уточнено**: это 7 pytest-*селекторов*, но **6 уникальных файлов** — `tests/test_embeddings.py` встречается дважды двумя разными классами (`TestEmbeddingsRequestResponse`, `TestEmbeddingsEndpointUnit`). Docstring `ci.yml` сам объясняет узость как сознательный выбор: «Docker-backed integration tests… stay out of this gate». |
| Противоречие в документации по пагинации | `docs/CARE_INTEGRATION.md:178` vs `:198` | Дефолт `sort_by=last_run_at`, но cursor «only valid with the default sort (`created_at asc`)». То есть при дефолтном листинге cursor не работает — только offset. Подтверждено дословно; дополнительно подтверждается `TODO.md:370-373` (ветка `who-cares`): «Cursor pagination only applies when sort matches its encoding (`created_at asc`); other sorts silently ignore the cursor and use offset». |
| Расхождение имён tool-фильтров | HTTP vs SDK | Query-параметры единственного числа (`requires_tool`), kwargs SDK множественного (`requires_tools`) — `docs/AGENT_SKILL_ENTITY.md:91-92` vs `:170-172`. **[ревизия 2026-07-29]**: `TODO.md:1637-1641` подтверждает, что асимметрия намеренная (итерация #22 сознательно называет SDK-метод плюралом, сериализуя в сингулярные query-параметры на проводе), не случайная опечатка — но сам `docs/AGENT_SKILL_ENTITY.md` нигде не поясняет разницу сноской. |
| `MemoryCardContent` — модель целиком не используется | `requests.py:127`, `responses.py:40` | `grep -rn "MemoryCardContent" --include="*.py" .` — только объявления в двух файлах, ни одного импорта; `memory_cards.py:14` импортирует лишь `EntityCreateRequest, EntityUpdateRequest`. Модель существует только как незадействованная документация-в-виде-кода. |
| `connected_ideas` — пустой слот | `requests.py:148`, `responses.py:61`, `search_document_service.py:134` | Единственное место в репозитории со словом «idea» (3 упоминания). Типизировано только как `list[dict[str, Any]]`, не заполняется ни одним сервисным путём, не тестируется, отсутствует в `openapi.yaml` и в `docs/`. `TODO.md` (2814 строк) тоже ни разу не упоминает «idea» (`grep -n "connected_ideas\|idea" TODO.md` → 0 совпадений). |
| `parents: ARRAY(UUID)`, но кроссовер (2+ родителя) невозможен на API | `db/models.py:148-172` (колонка), `entity_service.py:307,451-452,1097` (запись) | `evolution_meta.parent_version_ids` документирован как поддерживающий 2+ родителя для кроссовера (`requests.py:58-64`), но единственный вход в реальную колонку — скалярный `parent_version_id: str \| None`; ни один путь не пишет 2+ элемента. `/lineage` обходит именно `parents`, так что multi-parent происхождение декларировано в схеме, но невидимо для lineage-обхода. |
| ORM-модель не отражает часть реальной схемы | `db/models.py:148-172` (`EntityVersion`), `:175-218` (`EntitySearchDocument`) | Колонки `embedding` (обе таблицы) и `search_vector` (`entity_search_documents`) добавлены прямым SQL в миграциях 001/002, но отсутствуют как `Mapped`-поля в ORM-классах. |
| `FacetsResponse.authors` всегда `{}` | `unified_search.py:337`, `responses.py:368-372` | Поле объявлено в схеме (`dict[str, int]`), код явно комментирует «Empty for now — requires joining with entity_versions»; ни один код-путь не заполняет. |
| Скоупы `write:any`/`delete:any`/`admin:keys`/`evolve` не гейтят ничего | `auth.py:66-79` (объявление), `default_namespace_for:143-168` | Единственный реально проверяемый скоуп во всех роутерах — `clear:all` (`routers/entities.py:218`). Докстринг `default_namespace_for` обещает «the service layer enforces scope checks» — ни сервис, ни роутеры этого не делают. Подробности — раздел «Аутентификация и изоляция». |
| Мутации открыты без ключа даже при `AUTH_REQUIRED=true` | 13 файлов роутеров (см. раздел «Аутентификация и изоляция») | `Depends(require_api_key)` висит только на create/list; PUT/PATCH/DELETE/favourite/run-recorded/lineage/versions-beating/revert/pin/promote не проверяют ключ ни в одном роутере. |
| `steps` создаётся без auto-namespace-scoping | `steps.py:7` (импорт без `default_namespace_for`) | В отличие от остальных 4 типов (`agents.py:62`, `chains.py:165`, `agent_skills.py:124`, `memory_cards.py:36`), `namespace` для `step` пишется как есть из тела запроса, без auto-scoping на запись. |
| `SearchContext` — неиспользуемая абстракция | `search_strategies/base.py:97-180`, инстанцируется на `:196` | Ни одна из трёх стратегий поиска не читает `self.context`; SQL и сборка `SearchHit` продублированы инлайн в каждой. |
| `hybrid_default_bm25_weight`/`hybrid_default_vector_weight` — мёртвые настройки | `config.py:22-23` | Объявлены, нигде не читаются; реальный дефолт `(0.5, 0.5)` зашит в `models/requests.py:506,580` и `search_strategies/base.py:37`. |
| `ts_rank_cd`-комментарий про нормализацию неточен | `search_strategies/bm25_strategy.py:51` | Комментарий «divide by document length + 1»; флаг `32` у PostgreSQL означает `rank/(rank+1)`, не деление на длину документа. |
| `ApiKeyService.list_keys`/`revoke_key` — доступны только тестам | `api_key_service.py:127-157` | Нет HTTP-ручки и нет CLI для revoke/list; выпуск ключей — только через `api/app/create_key.py`. `list_keys` не вызывается вообще нигде, включая тесты. |
| Redis-паблишер событий без обработки ошибок | `events/publisher.py:29-56` (`.publish()` на `:56`) | Запись в Postgres уже закоммичена до похода в Redis; недоступный Redis превращает успешную запись в 500-ответ клиенту без отката и без возможности отличить «не записалось» от «записалось, но не долетело уведомление». |
| `deploy/nginx/README.md` — заглушка вместо конфига | `deploy/nginx/README.md` (77 байт) | Единственная строка: «Place nginx reverse proxy configuration here for production TLS termination.» Папка существует, реального nginx-конфига нет; верхнеуровневый README её вообще не упоминает. |
| Заявленная лицензия MIT юридически не установлена | `README.md:248-250` (текст «MIT»), файла `LICENSE` нет ни на `main`, ни на `origin/who-cares` | `gh api repos/AIRI-Institute/gigaevo-memory --jq '.license'` → `null`; ни в одном `pyproject.toml` поля `license` нет. Для форка/переиспользования правовой статус фактически не определён. |
| `.env.example` содержит флаги под нереализованные бэкенды | `deploy/.env.example` (`ENABLE_OPENSEARCH=false`, `ENABLE_MINIO=false`) | Ни разу не упоминаются больше нигде в README/CLAUDE.md/docs/TODO.md — заготовки без единой реализации или документации. |

---

## Общая оценка

Сервис **производственного качества по инженерии** (миграции с гейтом, скоупы, backpressure, Prometheus, бэкапы, doc-контрактные тесты), но **функционально это ровно хранилище + поиск**. Никакой «интеллектуальной» памяти — ни суммаризации, ни извлечения, ни консолидации, ни forgetting, ни графа знаний. Слово «memory» здесь означает «persistent store», а не «agent memory».

Ветка `who-cares` с `TODO.md` на 2814 строк показывает, что команда вела разработку крайне дисциплинированно: каждый пункт помечен приоритетом P0–P4, у выполненных стоит `[DONE]` с описанием, какие файлы затронуты, какие тесты добавлены и с каким результатом («16/16 passed»). Это лучший источник для понимания, что реально сделано, а что осталось.

**Ревизия 2026-07-29**: подтверждено точным измерением — `TODO.md` действительно 2814 строк (`git show origin/who-cares:TODO.md | wc -l`), 9 разделов, `grep -c '\[DONE' TODO.md` → **57** маркеров `[DONE`, и построчным прочтением всех 9 разделов не найдено ни одного пункта без `[DONE]` — весь документ от §1 до §9 отмечен как выполненный. Нюанс: «выполнено» не значит «без оговорок» — внутри уже-`[DONE]` блоков встречаются явно отложенные подпункты (`search_agent_skills` как отдельный метод, LRU-дедупликация `run_id`, клиентские стабы `cancel_evolution()`/`pause_evolution()`/`resume_evolution()` с `NotImplementedError`, `pgvector_index_size` в `/health`), и один раздел (§3, auth) изначально закрывался частично (итерация #25: «foundation + writes-side wiring; read-side scoping remains») и был доведён до полного соответствия только итерацией #41. То есть дисциплина реальна, но «всё DONE» — это финальное состояние документа, а не то, что каждый пункт был закрыт с первой итерации.

---

## Открытые вопросы

1. **Публичное зеркало vs. приватный монорепо.** `main` — один orphan-коммит, генерируемый из приватного `gigaevo-memory-internal` (см. `.github/workflows/mirror.yml`). Ветка `who-cares` — очевидно, случайно опубликованный срез реальной истории с `TODO.md` на 2814 строк. **Вопрос**: полагаться ли на `who-cares` как на источник контекста и стоит ли предупредить команду AIRI? Ветка может быть удалена в любой момент — стоит сохранить локальный клон.
2. **Что такое `connected_ideas` в замысле авторов?** Поле объявлено в обеих Pydantic-моделях, попадает в поисковый документ, но не типизировано, не заполняется, не документировано, не упоминается в `TODO.md`. Был ли это задел под что-то конкретное, или мёртвое поле, унаследованное от gigaevo-core?
3. **Кто и как порождает `memory_card` в живом контуре GigaEvo?** Лендинг говорит «после завершения эксперимента успешные решения автоматически сохраняются в память», но кода, который бы формировал карточку (описание, `explanation`, `keywords`), в этом репозитории нет — значит, он в `gigaevo-core` или `gigaevo-platform`. **Это самое важное для Проекта 28**: если в `gigaevo-core` уже есть LLM-пайплайн генерации карточек из результатов эксперимента, его надо найти и не писать заново. `FusionBrainLab/gigaevo-core` — публичный, проверить в первую очередь.
4. **Совместим ли `memory_card` с «идеей» семантически?** Схема заточена под «паттерн, найденный эволюцией программ»: `program_id`, `programs[]`, `code`, `last_generation`, `fitness`. Для идей в смысле Ideas Lake половина полей пуста. Стоит ли это принять (пустые поля дешевы в JSONB) или заводить свой тип?
5. **Что даёт `mmar-carl` в dev-зависимостях API?** Заявлен в `api/pyproject.toml` extras `dev`, но `import mmar_carl` в `api/app` отсутствует. Вероятно, нужен только тестам (`_validate_carl_dag` в `chains.py:36`?). Проверить, есть ли скрытая связь с форматом цепочек.
6. **Планируется ли `agent_skill`-подобный тип для идей?** `docs/EVOLUTION_META.md:221-223` прямым текстом допускает расширение lineage на другие типы. Есть ли у команды AIRI такой план — и не дублирует ли его Проект 28?
7. **Насколько актуален публичный срез?** Последний push — 26 июня 2026, то есть месяц назад. Приватный репозиторий за это время наверняка ушёл вперёд. Стоит ли договариваться о доступе к `gigaevo-memory-internal` или к телеграм-каналу команды?
8. **Дефолтный `VECTOR_DIMENSION=384` фиксируется в миграции**, а не в рантайме (`001_initial.py:109`, `002:107` используют `settings.vector_dimension` на этапе `alembic upgrade`). Смена модели эмбеддингов на другую размерность требует новой миграции с пересозданием колонки и переиндексацией. Учитывать при выборе русскоязычной модели (у `multilingual-e5-large` — 1024).
9. **Лицензия и правовой режим.** `README.md:250` заявляет MIT, но файла `LICENSE` в репозитории нет. Для внешнего проекта, который будет форкать/переиспользовать код, это надо прояснить.
10. **Web UI на Gradio** (`web_ui/`) — насколько он вообще нужен Ideas Lake? Половина вкладок закомментирована; вероятно, проще написать свой фронт, а `web_ui/app/client.py` (513 строк) взять как reference-имплементацию работы с API.

### Что изменилось к 2026-07-29

1. Не отвечено полностью, но уточнено: механизм зеркалирования теперь разобран целиком (`mirror.yml` — триггер `workflow_run` по CI ветки `release` приватного репозитория, orphan-коммит, форс-пуш секретом `MIRROR_TOKEN`), имя приватного репозитория подтверждено — `gigaevo-memory-internal` (из коммита `who-cares` `c17f5d75`, а не из самого `mirror.yml`, который себя не описывает). `who-cares` на момент разбора всё ещё существует как `origin/who-cares` (только remote, 11 коммитов, последний `d3475c51` от 2026-06-11). Риск удаления ветки и вопрос предупреждения команды AIRI остаются открытыми.
2. Не отвечено, но подкреплено новыми данными в пользу гипотезы «мёртвое поле»: вся модель `MemoryCardContent`, где объявлен `connected_ideas`, не используется нигде за пределами файла своего определения (ни одного импорта в `memory_cards.py` или где-либо ещё), и `TODO.md` (2814 строк) ни разу не упоминает ни `connected_ideas`, ни «idea» ни в каком написании.
3. Не отвечено — ни часть A, ни часть B, ни часть C не касаются `gigaevo-core`/`gigaevo-platform` (вне зоны клона). Остаётся приоритетным для отдельной проверки.
4. Не отвечено прямо, но фактура плотнее: `connected_ideas` — пустой слот, `parents` технически `ARRAY(UUID)`, но кроссовер (2+ родителя) не проходит через API ни в одном пути записи, `/lineage` есть только у `chain`. Совместимость `memory_card` с «идеей» выглядит скорее частичной, чем полной.
5. Не отвечено — ни один из трёх разборов не исследовал `mmar-carl`/`_validate_carl_dag`, вопрос остаётся полностью открытым.
6. Не отвечено — часть C дословно переподтвердила цитату `EVOLUTION_META.md:221-223`, новых сведений о планах AIRI не найдено.
7. Отвечено количественно: `gh api repos/AIRI-Institute/gigaevo-memory --jq '.pushed_at'` → `2026-06-26T14:06:13Z`, то есть **33 дня** до даты разбора (2026-07-29), не «месяц» приблизительно. `who-cares` остановилась ещё раньше — 2026-06-11 (15 дней до последнего пуша `main`). Договорённость о доступе к `gigaevo-memory-internal` или телеграм-каналу команды не подтверждена — вопрос остаётся открытым.
8. Уточнено содержательно: `VECTOR_DIMENSION` фиксируется не как хардкод-константа в файле миграции, а как значение `settings.vector_dimension` (дефолт `384`), подставляемое в f-строку SQL **в момент запуска** `alembic upgrade` (`001_initial.py:107-110`, `002_memory_card_search_documents.py:107-112`). Практический вывод тот же (смена размерности эмбеддингов требует новой миграции и переиндексации), но механизм — конфиг на этапе применения, а не константа в исходнике.
9. Отвечено: `LICENSE` действительно отсутствует и на `main`, и на `origin/who-cares` (`git show ...:LICENSE` → `fatal: path 'LICENSE' does not exist` для обеих веток); `gh api repos/AIRI-Institute/gigaevo-memory --jq '.license'` → `null`; ни в одном из трёх `pyproject.toml` нет поля `license`. Вопрос закрыт фактически — MIT не установлена юридически, прояснение с командой AIRI нужно.
10. Не отвечено по существу (нужна ли своя замена), но подтверждены оба факта: вкладки Steps/Agents закомментированы (`web_ui/app/main.py:86-89`), `web_ui/app/client.py` — ровно 513 строк (`wc -l`).

Новые вопросы, возникшие при разборе частей A/B/C:

- CI реально гоняет 6 уникальных файлов из 60 (не 7, как казалось по числу селекторов) — auth, поиск, SSE, дедупликация вне публичного гейта. Приемлемо ли такое покрытие как ориентир для собственного CI блока A, или в Ideas Lake нужен более широкий обязательный набор с самого начала?
- Множественные скоупы (`write:any`, `delete:any`, `admin:keys`, `evolve`) объявлены, но не гейтят ни одной ручки, и почти вся типовая поверхность API (все мутации) вообще не требует ключа даже при `AUTH_REQUIRED=true`. Это осознанная граница доверия CARE-экосистемы (внутренняя сеть, доверенные клиенты) или недосмотр, который не стоит копировать в собственный контракт retrieve/ingestion?
- `parents: ARRAY(UUID)` при фактически единственном родителе на всех путях записи — стоит ли в собственной модели версии сразу проектировать multi-parent lineage как реально достижимый (не только в типе колонки), если Ideas Lake планирует поддерживать слияние идей из нескольких источников?
