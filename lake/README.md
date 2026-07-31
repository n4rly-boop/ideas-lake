# Блок A — Ingestion + Retrieve

Реализация спеки `knowledge/10-implementation-spec.md` (локальная, в репозиторий не идёт).
Источник → тезис → идея на записи; запрос → выдача на чтении. Граф — блок B, эволюция — C, стоимость — D.

Зависимости: `fastapi`, `uvicorn`, `pydantic`, `numpy`, `sentence-transformers`, `PyYAML`,
`neo4j` + stdlib. `neo4j` — Bolt-over-TLS из stdlib не адресуется, обоснование в `CLAUDE.md`.
Исходящий HTTP (arXiv, llama.cpp) — `urllib.request` из stdlib.
Python 3.12. Платных API нет: LLM — серверы школы (llama.cpp), эмбеддинги — локально на CPU.

**Отклонение от спеки, названное явно:** §5.4 выбрала `http.server` ради нуля зависимостей
(`09:290`). Взяли FastAPI ради типизированных моделей запроса/ответа и OpenAPI-схемы для C.
Весь стек уже стоял в окружении. `pydantic` — доменные модели тоже на нём;
**схемы для LLM остаются литеральными dict'ами** и из моделей не генерируются:
`model_json_schema()` даёт `$ref`, на котором грамматика llama.cpp молча не собирается (`09:67`).

---

## 1. Точки входа

| Команда | Что делает |
|---|---|
| `python3 -m lake.ingest.run phase1 [--limit N] [--sources path]` | fetch → parse → generalize → `data/staging.jsonl`. 8 потоков. **В граф не пишет ничего** |
| `python3 -m lake.ingest.run phase2 [--limit N]` | staging → линковка → граф + индекс → пере-вывод → свод судьи доверия по грязным идеям (`13` §3). Последовательно, курсор |
| `python3 -m lake.ingest.run selfcheck` | офлайн end-to-end на фикстурах, временные БД |
| `python3 -m lake.ingest.runlog <evolution_full.csv> [--limit N] [--min-abs-delta X] [--dry-run]` | лог прогона эволюции (GigaEvo) → `data/run/{run_id}.jsonl` → тот же конвертер, что и `POST /run` (`13` §2.5). `--dry-run` — отчёт без записи файла |
| `python3 -m lake.api.app [--port 8077] [--host H] [--mock] [--no-auth]` | FastAPI-сервер под uvicorn: граф, поиск, ингест, retrieve, research. Нужен `LAKE_API_KEY`, иначе не поднимется |
| `uvicorn lake.api.app:app --port 8077` | то же штатным способом |
| `python3 -m lake.api.selfcheck` | офлайн-проверка HTTP-слоя (она же `--selfcheck`) |
| `python3 -m lake.research.selfcheck` | офлайн-проверка research boundary без сети и ключей |
| `python3 -m lake.selfcheck [--offline]` | проверки §6, точный счёт и текущий статус — конец §6. `--offline` пропускает канарейку (единственный сетевой пункт) |
| `python3 tools/gen_sources.py` | `09-raw/a11-sources.yaml` → `lake/sources.yaml` (84 записи) |
| `docker compose --env-file .env.local up -d` | локально: соберёт образ сам. На сервере образ приезжает из GHCR, см. ниже |

**CI/CD** — `.github/workflows/deploy.yml`, срабатывает на push в `main`, если тронуты
`lake/**`, `Dockerfile`, `docker-compose.yml` или сам workflow. Порядок: собрать образ →
**прогнать три офлайн-проверки внутри собранного образа** (не в окружении раннера: иначе
зелёный прогон говорит про питон раннера, а не про то, что поедет) → выложить в
`ghcr.io/n4rly-boop/ideas-lake` тегами `latest` и коротким sha → по ssh обновить сервер →
**дождаться `healthy`**, иначе прогон красный. Без последнего шага CI зеленел бы над
мёртвым сервисом: `up -d` возвращает 0, как только контейнер создан, а не когда отвечает.

Сервер исходников не держит и не собирает: приезжает готовый образ плюс один
`docker-compose.yml`. Логин в GHCR делается токеном самого прогона и живёт минуты —
долгоживущего PAT на машине нет. `.env.local` в CI не приезжает никогда.
Откат — `LAKE_TAG=<старый sha> docker compose --env-file .env.local up -d`, без пересборки.

Шаг `pull & up` (`deploy.yml`) пишет `LAKE_TAG=<sha>` в серверный `.env.local` —
идемпотентно (заменяет строку `sed`, не плодит дубли), правкой на месте, не
перезаписью файла с секретами целиком. Поэтому и ручной `docker compose up`/`run`
на сервере без флагов резолвит тот же образ, что и последний деплой, а не то, что
подставит переменная по умолчанию. Дефолт в `docker-compose.yml` — `${LAKE_TAG:-local}`,
не `:latest`: `latest` в GHCR двигает сам CI при каждой сборке, и если `LAKE_TAG`
когда-нибудь пропадёт из `.env.local`, `pull` на несуществующий тег `:local` упадёт
явно, а не подставит образ неизвестной свежести молча. Разобрано на живом сервере
2026-07-31: контейнер бежал на `f9ebcd10efc9`, а `docker compose run` без `LAKE_TAG`
тянул `:latest` и падал `No module named lake.idea_edges` — старый образ, писавший
граф в SQLite, поверх данных, которые уже в Neo4j.

Каждый модуль дополнительно исполняем: `python3 -m lake.index`, `python3 -m lake.embed`,
`python3 -m lake.ingest.link`, `python3 -m lake.ingest.trust`, `python3 -m lake.ingest.runlog`,
`python3 -m lake.neo4j_store`, `python3 -m lake.graph_client`, `python3 -m lake.retrieve.rank`
и т.д. — свой `__main__` self-check. `graph_client` требует живого Neo4j на `bolt://localhost:7687` (т.е. `docker compose` должен быть запущен). `neo4j_store` тоже требует графа. Остальные работают без сети.

**Ключи** читаются из окружения в момент вызова, не на импорте: `LAKE_KEY_9B`, `LAKE_KEY_35B`.
Без них модули импортируются и не падают на импорте — сколько проверок при этом реально
проходит, см. конец §6 (число нестабильно прямо сейчас, см. там же). Локально:
`set -a; . ./.env; set +a` (`.env` в `.gitignore`, ключи в репозиторий не попадают).

---

## 2. Раскладка

```
lake/
  models.py         # Source / Thesis / Idea (pydantic) + JSON-схемы для LLM + пути + хеши
  llm.py            # клиент llama.cpp: схема, канарейка, fail-closed
  embed.py          # snowflake-arctic-embed-s, 384d, на CPU
  trace.py          # C5: JSONL-трейс каждого вызова
  index.py          # индекс тезисов: FTS5 + numpy + RRF. Мой навсегда, на Neo4j не едет
  graph_client.py   # ЕДИНСТВЕННОЕ место, знающее формат B; Neo4j единственный бэкенд (D11, 2026-07-31), NEO4J_URI обязателен
  neo4j_store.py    # Neo4j-бэкенд: Cypher, пишет рёбра A (cocitation в фазе 2, derived_from при синтезе, D12, 2026-07-31)
  queue.py          # долговечная очередь /fetch и /run: своя SQLite-база data/jobs.db, переживает рестарт
  writer_lock.py    # flock на data/writer.lock: один писатель фазы 2 на озеро, поверх процессов
  selfcheck.py      # assert-проверки офлайн-слоя, один запуск — точный счёт и статус см. §6
  sources.yaml      # сгенерирован маппером
  ops.py            # составные операции: то же, что ручки, но вызываемо импортом
  vault.py          # выгрузка в Obsidian-vault (спека 11): граф рисует Obsidian, не мы
  neo4j_load.py     # односторонняя заливка в Neo4j блока B; уйдёт, когда B сдаст адаптер
  ingest/  fetch parse generalize link rederive split run trust runlog
                    # trust.py  — судья 35B: idea + <=16 листьев -> trust_score 0..10, пишет через set_trust (D14, 2026-07-31)
                    # runlog.py — конвертер логов эволюции GigaEvo: CSV/HTTP -> staging фазы 1 (D14, 2026-07-31)
  retrieve/ rewrite search rank api        # api.py — ядро чтения, без транспорта
  research/ models agent web              # RAG + self-hosted web → language report
  api/     app routes schemas jobs workers selfcheck   # HTTP-слой на всё, единственный сервер
                    # workers.py — пул фазы 1 + единственный писатель фазы 2 поверх queue.py
  prompts/{parse,generalize,link,rederive,rewrite,trust}/system.txt
  prompts/generalize/run.txt   # обобщение лога эволюции — свой список запретов на утечку (`13` §2.2.1)
  data/             # gitignored: raw/ cache/ traces/ logs/ fetch/ run/ staging.jsonl index.db lake.db
                    #   jobs.db — очередь /fetch и /run (queue.py); writer.lock — flock единственного писателя
                    #   run/{run_id}.jsonl — staging одного прогона эволюции, свой курсор (`13` §2.3)
```

---

## 3. Как работает запись

Две фазы с файлом между ними (§4.7). Разделение не косметическое: фаза 2 — 25 минут
последовательных вызовов, и падение на середине в сквозном варианте стоило бы всего прогона.

```
ФАЗА 1  (8 потоков, граф не открывается ни разу)
  sources.yaml → fetch (HTML → ar5iv → PDF, пауза 3 с, кэш data/raw/)
               → parse секции (9B, потолок 6/секция, 30/документ)
               → generalize (9B) + автопроверка утечки конкретики
               → вектор → строка в data/staging.jsonl

  ▲ ПРИЁМКА: тезисы читаются глазами, промпт правится, фаза 1 перегоняется. Граф чист.

ФАЗА 2  (последовательно, курсор data/staging.cursor)
  на источник: write_source
    → link каждого тезиса:
        [0] text_hash уже в этом source_id? (хранилище ∪ батч-оверлей) → пропуск, 0 вызовов
        [1] кандидаты: index.search_theses(k=30) ∪ оверлей → top-10 различных идей
        [2] арбитр 35B → индекс кандидата | -1
        [3] link | new; решение сразу в оверлей. Сбой → pending_link, тезис НЕ пишется
    → create_idea_with_theses одной транзакцией
    → index.index_theses тем же шагом (иначе индекс разъедется с графом)
    → split идей, у которых листьев > 16 (issue #2): кластеризация векторов листьев,
      каждая часть пере-выведена ДО единственной транзакции, потом реиндекс
    → rederive идей, у которых len(leaves) - rederived_at_leaf_count >= 3
    → курсор
  ПОСЛЕ ЦИКЛА, один раз за проход (`13` §3.2-3.3):
    → судья доверия по всем `dirty` идеям хранилища (не только тронутым этим проходом),
      первые LAKE_TRUST_PER_PASS (умолч. 50): score 0..10 от 35B -> trust_score,
      dirty снимается ТОЛЬКО здесь (graph_client.set_trust, одним UPDATE);
      отказ судьи -> счёт не пишется, dirty остаётся, идея ждёт следующего прохода
```

`dirty` поднимается в той же транзакции, что и запись листьев
(`create_idea_with_theses`), и больше нигде — ни один другой писатель его не трогает
(`rederive.maybe_rederive` переписывает, что идея ГОВОРИТ, и намеренно не лезет в то,
чего она СТОИТ). Единственное место, где флаг снимается, — `graph_client.set_trust`,
и оно же пишет число: одна операция, чтобы идея не могла оказаться «чистой» со старым
счётом. Триггер пере-вывода — отдельный: `dirty AND (leaves - rederived_at_leaf_count
>= 3)`, флаг — дешёвый предфильтр, счётчик — порог.

**`POST /run` — приём прогона эволюции GigaEvo, тем же путём, что и корпус, но батчем
мутантов, а не URL (`13` §2).** Один `Source` на мутант с вычислимой дельтой фитнеса
(`type="run"`, `version=program_id`), один `Thesis` на `changes[i]` мутационного вывода.
Батч едет файлом под `data/run/{run_id}.jsonl` (не в теле задания — до сотен КБ на
поле `args`, которое эхом уходит на каждый опрос `/ingest/jobs`), дедуп — по `run_id`.
Конвертер (`lake/ingest/runlog.py`) — общий для CLI и HTTP: CSV → тот же payload, что
принимает `/run` → строки staging ровно формата фазы 1 → `run.phase2` без изменений.
`-1000.0` в `metric_fitness` — маркер отказа валидации, не число (`13` §1.3): в
`run_success`, в `effect`, в дельте он никогда не участвует. Отсевы считаются
поимённо, не одним «пропущено»: `dropped_dead`, `dropped_no_fitness`, `dropped_root`,
`dropped_parent_unmeasured`, `dropped_min_delta`, `rows_unparsed`, `mutants_no_changes`.

**`POST /fetch` — те же две фазы для одной ссылки, в одном задании.** Приёмка глазами —
свойство корпусного прогона, а не пути записи: кто прислал ссылку, тот ждёт статью в графе.
Статья получает собственный `data/fetch/{id}.jsonl` и собственный курсор — на общем файле
фаза 1 сбросила бы корпусный курсор, и следом идущая фаза 2 залила бы в граф все источники,
которые ещё ждут приёмки. Слот, арбитр, `pending_link` и триггер пере-вывода — те же.
Повторный `POST` той же ссылки идемпотентен: тот же `Source.id`, шаг [0] пропускает
записанные листья.

**Очередь `/fetch` — `lake/queue.py`, своя SQLite-база `data/jobs.db`, живёт отдельно от
графа Neo4j и переживает рестарт процесса** (в отличие от словаря `api/jobs.py`,
который умирает вместе с процессом). Статусы и переходы:

```
queued --claim--> running(stage=phase1) --stage()--> staged
staged --claim--> running(stage=phase2) --finish()--> ok | failed
                                        --release()--> staged   (писатель занят чужим)
```

`POST /fetch` кладёт задание в `queued`; дедуп — по `arxiv_id` (`dedup_key`): повтор
ссылки, которая уже `queued`/`running`/`staged`, возвращает то же задание, а не открывает
второе. Задание, упавшее посреди фазы, получает попытку заново (`retry`) — три раза
(`MAX_ATTEMPTS = 3`), после чего остаётся `failed` с причиной на строке. Завершённые
задания (`ok`/`failed`) держатся кольцом — `KEEP_FINISHED = 200` последних, старше
вытесняются. При рестарте процесса `queue.recover()` возвращает всё, что осталось
`running`, назад на статус, с которого его забрали (`phase1` → `queued`, `phase2` →
`staged`) — поток, который его вёл, погиб вместе с процессом, а работа возобновляема:
фаза 1 перечитывает источник заново (кэши `data/raw/` и `data/cache/` — скачанный
текст и ответы парсера по паре (хэш секции, хэш промпта) — делают повтор дешёвым), фаза 2 — с
курсора.

Пул фазы 1 (`lake/api/workers.py`, `FETCH_WORKERS` потоков, по умолчанию 2 — калибровочная
ручка под общий с остальной школой пул 9B, не под своё CPU) забирает `queued`-задания и
гоняет `run.stage_one`: граф не открывается ни разу. Писатель фазы 2 — **ровно один**
поток (`run.drain_one`), забирает `staged` и линкует в граф. «Ровно один» держится тремя
независимыми гарантиями, ни одна не достаточна сама по себе:

1. один поток-писатель на процесс, стартует один раз из `lifespan` приложения;
2. `jobs.exclusive("fetch")` вокруг фазы 2 — тот же слот, что и у ручных `/ingest/phase2`
   и `/admin/reindex` (§4.5), поэтому писатель и ручной прогон не пишут одновременно;
3. `flock` (`lake/writer_lock.py`) — второй **процесс** (второй `uvicorn --workers`,
   перекрывающий деплой, `python3 -m lake.ingest.run phase2` с хоста) получает отказ:
   двух держателей слота (2) внутри разных процессов ничто не свяжет друг с другом, и
   озеро завело бы по два дубля идей на каждый механизм. Замок берётся **на каждом входе
   в фазу 2** — и в `workers.start()` на время жизни писателя, и внутри самой
   `run.phase2`, потому что CLI и `/ingest/phase2` идут мимо `workers.py`. Он
   реентерабельный ровно для вложенности `write_step → drain_one → phase2`.

Если писатель занят слотом (ручной `/ingest/phase2` или `/admin/reindex`), `queue.release`
возвращает задание в `staged` без траты попытки — занятый слот не поломка.

Попытки считаются **по стадии**: `stage()` обнуляет счётчик, потому что фазы — две разные
работы, и статья, которой понадобилось два скачивания, иначе входила бы к писателю с одной
жизнью. Постоянная ошибка (`FetchError` — HTML нет ни по одному из трёх путей; `ValueError` —
парсер не достал тезиса) кладёт задание в `failed` **с первой попытки**: три круга
fetch/parse ради того же ответа стоят пула три круга, а причину прячут под «attempt 3 of 3».
Временную (канарейка, 503 от школьного пула) повторяем.

`workers.start()` берёт `flock` **до** `queue.recover()`: `recover()` объявляет мёртвым всё,
что осталось `running`, и процесс, сделавший это первым, вернул бы в очередь задания, которые
прямо сейчас гоняет держатель замка. `workers.stop()` не отпускает замок, пока писатель
внутри фазы 2 (join — секунды, фаза 2 — минуты), и не вычищает из `alive()` живые потоки:
иначе `/healthz` показывает застой на работающем ингесте.

**Три способа кончить неизменившимся озером — все три `status: failed` с причиной, не `ok`:**
мёртвая ссылка; статья, из которой парсер не достал ни тезиса; арбитр линковки, отказавший
на **всех** тезисах (тогда каждый лежит в `pending_link`, в графе нет ничего). Третий отличим
от легального повтора только потому, что отчёт фазы 2 считает отказы арбитра отдельно от
дублей — `theses_refused` против `theses_skipped`; одним счётчиком эти два случая
неразличимы побайтно. Канарейки **обеих** моделей — до фетча, чтобы мёртвый 35B не стоил
полного разбора статьи.

Свой `data/fetch/{id}.jsonl` удаляется после успешного залива: в каталоге остаются ровно те
статьи, которые не доехали, — ни `/ingest/staging`, ни `/stats` их не показывают, они читают
только корпусный файл.

Порога косинуса на линковке **нет** — решает всегда арбитр, «дубля нет» говорит сентинелом `-1` (§0.6).
Батч-оверлей — условие корректности, а не оптимизация: без него тезис №2 не видит идею,
созданную тезисом №1, и одна статья заводит два дубля под один механизм (§0.1.13, `link.py`).

Отчёт фазы 2: источники, тезисы, идеи, доля идей с ≥2 источниками, длина `pending_link`,
доля утечек, **число идей без листьев** (теперь легально для гипотез — `origin =
"synthesized"`, считается отдельно от `hypotheses`; идея-НЕ-гипотеза без листьев — это
по-прежнему поломка записи, `13` §5), токены и время из трейсов. Пропуски разведены на
два числа: `theses_skipped` — дубль, лист уже в озере; `theses_refused` — арбитр отказал,
лист нигде и ждёт в `pending_link`. Это противоположные исходы, и одним счётчиком они
читаются одинаково.

Свод судьи доверия дописывает в тот же отчёт (`lake/ingest/trust.py`, `13` §3.3, ключи
из кода, не пересказ): `trust_scored` — идей, на которые судья реально ответил;
`trust_failed` — отказов (счёт не изменился, идея осталась `dirty`); `trust_errors` —
список `{idea_id, error}` для них; `trust_leaves_capped` — сколько раз показ листьев
судье срезался потолком в 16; `trust_mean` — среднее по оценённым в этом проходе;
`trust_due` — сколько идей было `dirty` на входе в свод; `trust_deferred` — сколько из
них не попало под потолок `LAKE_TRUST_PER_PASS` (умолч. 50) и осталось на следующий
проход. `trust_failed` и `trust_deferred` — разные вещи и не сворачиваются в одно число:
первое — судья ответил и ответ не принят, второе — до идеи в этом проходе просто не
дошли.

---

## 4. Как работает чтение

```
POST /retrieve
  → rewrite (9B, 20 с): запрос «в терминах решения». Отказ НЕ фатален → сырой запрос + rewrite_failed
  → search: BM25 (FTS5) + косинус (numpy), слияние RRF k=60, top-50 тезисов
  → rank: thesis_id → idea_id → dedup по МАКСИМУМУ скора
           → raw_score сохраняется как есть
           → нормировка в [0,1] по ПОЛНОМУ списку кандидатов
           → score = norm_score + TRUST_WEIGHT · trust_norm (шкала фиксирована 1.0;
             TRUST_WEIGHT = 0.0 с 2026-07-31 — доверие теперь от судьи и ещё не
             откалибровано, см. §8 п.8)
           → отбор сверху вниз, но не более floor(0.2·k) идей с trust_score == 0
             в выдаче (D14, `TRUST_QUOTA_FRACTION`) — квота, не вес: отсеянные
             недоверенные не выбрасываются, а ждут внизу на случай нехватки
           → мало идей → neighbors(hops=1), via="edge" (рёбра реальны с D12) →
             дозаполнение, via="padding" → если доверенных так и не хватило —
             добор недоверенными СВЕРХ квоты, а не более короткая выдача
             (recall-first сильнее квоты); факт добора — в логе
           → cosine_similarity = query_vec · idea.vector (собственный, §1.3) — абсолютный,
             не перенормируется по вызову (см. п.1 ниже)
  → лог в data/logs/retrieve.jsonl: score, raw_score, cosine_similarity, via, cut_off,
    rewrite_failed, cost, trust_quota, untrusted_returned, untrusted_over_quota (D14)
```

Запрос обязан пройти `fts_escape()` перед `MATCH`: у FTS5 своя грамматика **и неявный AND**,
из-за которого 10-словный переписанный запрос вернул бы пустое BM25-плечо, а гибрид молча
выродился бы в чистый косинус (§5.2, `index.py:71`).

**Граница отказа.** Граф недоступен → **HTTP 503** `{error, log_id}`. Пустая выдача при живом
графе → **200** и `ideas: []`. Это данные для A/B, смешать их значит загрязнить главную метрику (§5.4).

---

## 5. Слой API

### 5.1 HTTP — единственная зависимость соседей от блока A

Один сервер на весь блок: `lake.api.app`. Скриптов, которые надо запускать руками на машине с
данными, не осталось — ингест, чтение графа, поиск и починка индекса ходят через HTTP.

`GET /docs` — Swagger, `GET /openapi.json` — машинная схема (422 из неё убран: мы его не отдаём).

**Ключ обязателен на всех ручках:** `Authorization: Bearer $LAKE_API_KEY`, иначе `401`
(`{"error": ...}` + `WWW-Authenticate: Bearer`). Проверка — middleware в `app.py`, а не
зависимость на маршруте: маршрут можно написать без зависимости, а эти маршруты пишут в
граф и тратят GPU школы. Она стоит **до** роутинга, поэтому несуществующий путь тоже `401`:
чтобы узнать, какие пути есть, ключ уже нужен. Без ключа в окружении сервер **не стартует** —
пустая строка это отказ на старте, а не «аутентификация выключена»; выключается явно,
флагом `--no-auth`, и он пишет об этом в лог. Открыты `/openapi.json` и `/docs` — контракт
интеграции, данных озера в нём нет.

| | |
|---|---|
| `POST /retrieve` | контракт C3: запрос → идеи с провенансом (ниже) |
| `POST /research` | bounded language mission → Lake priors + independent web evidence → report; не создаёт локальные идеи |
| `GET /search?q&k` | сырой гибрид по тезисам: BM25 + косинус, RRF. Без переписывания и без идей |
| `GET /sources`, `GET /sources/{id}` | постранично; `total` считается тем же фильтром и тем же JOIN, что и страница |
| `POST /sources` | upsert: сюда блок C пишет исход прогона. id = f(url, version), повтор заменяет строку. Повтор с другим `title`/`type` → `409`: это провенанс уже записанных листьев |
| `GET /ideas`, `GET /ideas/{id}` | идеи с листьями; `?include_vector=true` — 384 float, иначе их нет в ответе |
| `PATCH /ideas/{id}` | правка полей. Меняешь `text` — сервер пересчитывает вектор (§1.3), порознь нельзя |
| `GET /ideas/{id}/theses`, `/neighbors` | листья и рёбра. Рёбра — co-citation и derived_from, A пишет в пайплайне (D12, 2026-07-31) |
| `GET /theses?idea_id&source_id`, `GET /theses/{id}` | листья постранично |
| `POST /fetch` | одна статья arXiv по ссылке: обе фазы в одном задании, кладёт в очередь (`data/jobs.db`) и отдаёт `202` + задание, статус `queued`. В теле только `url` (`/abs/`, `/pdf/`, `/html/`, версия уважается). Не-arXiv ссылка → `400` на входе, до фетча и трат на LLM; **старый формат id** (`hep-th/9901001`) тоже `400` и с указанием причины: `fetch_metadata` теряет класс архива, и все три пути фетча отвечают 404. Свой `data/fetch/{id}.jsonl`: корпусный файл приёмки не трогается. **Никогда `409`** — слот один на писателя, но у `/fetch` перед ним очередь, а не отказ; дедуп по `arxiv_id` возвращает уже существующее задание вместо второго. Переполнение очереди (`LAKE_QUEUE_MAX`) → `429` + `Retry-After` |
| `POST /run` | прогон эволюции (GigaEvo) батчем мутантов, тем же путём, что `/fetch` (`13` §2.5): `202` + задание, `queued`, очередь `data/jobs.db`. Тело — `{run_id, task_id?, mutants: [...]}` (`RunRequest`/`MutantIn`, `api/schemas.py`), каждый мутант — `mutation_output` уже разобранным **или** `mutation_output_raw` строкой JSON как в CSV, конвертер разбирает сам. Батч пишется файлом под `data/run/{run_id}.json` (не в `args`: там он эхом уходил бы на каждый опрос `/ingest/jobs`), удаляется только когда фаза 1 приняла его. Дедуп — по `run_id`: повтор той же ссылки/тела возвращает то же задание, а **другое** тело под тем же `run_id`, пока задание живо, — `409` (`DedupConflict`, не как у `/fetch`, где второе тело такого дедупа не бывает). Переполнение очереди → `429` + `Retry-After` |
| `POST /ingest/phase1`, `POST /ingest/phase2` | `202` + задание; слот один на процесс, второй запуск → `409`. Без изменений — очереди `/fetch` не касаются |
| `GET /ingest/jobs`, `/jobs/{id}` | объединение двух регистров: очередь `/fetch` с диска (`data/jobs.db`, переживает рестарт) + ручные задания этого процесса (`phase1`/`phase2`/`reindex`/`vault-export`, живут в памяти). Статус: `queued` \| `running` (+ `stage`: `phase1`/`phase2`) \| `staged` \| `ok` + отчёт \| `failed` + текст ошибки. `/jobs/{id}` сначала смотрит в очередь — после рестарта только там и остался след |
| `GET /ingest/staging` | что лежит между фазами: строк, курсор, разбивка по источникам |
| `GET /ingest/pending-link` | очередь отказов арбитра (§4.5) — работа, которая ждёт, а не потеряна |
| `GET /healthz` | живость плюс инвариант «индекс == хранилище» (§6.19), который тухнет молча |
| `GET /stats` | числа отчёта §4.7 по всему озеру |
| `POST /admin/reindex` | пересборка индекса из хранилища: путь починки §6.19 |
| `POST /vault/export` | выгрузка озера в Obsidian-vault (спека 11). `dest` по HTTP не принимается: сервер слушает `0.0.0.0`, и «куда писать» было бы примитивом «пиши куда угодно». `--dest` остался в CLI |

Ручек на запись тезиса нет и не будет: тезис неизменяем (§1.2) и создаётся только фазой 2,
которая назначает `idea_id` через арбитра. Ручной лист прошёл бы мимо линковки. Удаления нет ни у чего.

`404` там, где пустой список соврал бы: несуществующая идея не отвечает `[]` на `/theses` —
идея без листьев это сломанный инвариант (`06:85`), и он не должен выглядеть как «нет такой идеи».

Тело ошибки — всегда `{"error": ...}`, на любом статусе, включая `404` на неизвестный путь и
`405` на неверный глагол (обработчик висит на классе Starlette, не на подклассе FastAPI — иначе
эти два отвечают `{"detail": ...}`, единственным телом, которое C не разбирает). Необработанное
исключение — тоже `{"error": "<Тип>: <текст>"}` с `500`, а не `text/plain`.
Что значит «хранилище упало», знает `graph_client.STORE_ERRORS`, а не слой API: иначе в день
переезда на Bolt все графовые ручки тихо съехали бы с `503` на `500`.

OpenAPI перечисляет ровно то, что ручка отдаёт: `422` убран (мы его не возвращаем), `400`
проставлен там и только там, где есть что валидировать, `503` — на всех графовых. Проверка
держит эквивалентность в обе стороны: лишний статус в схеме — та же поломка, что и недостающий,
C пишет ветку, которая никогда не сработает.

### 5.2 Deep research

`POST /research` — отдельная потребительская граница Ideas Lake. Запрос содержит
`query`, bounded `context`, полный список известных идей и направления, которые
нужно исследовать. Агент:

1. получает небольшой список карточек через локальный `/retrieve` только для
   gap/duplicate analysis;
2. планирует до пяти разных запросов (при сбое модели использует deterministic
   coverage fallback);
3. параллельно ищет в SearXNG и читает страницы через Crawl4AI, а PDF/arXiv —
   через Docling;
4. возвращает language report, `queries`, independently fetched `sources` и
   per-round token/latency cost.

Планирование запросов и synthesis в `lake/research/agent.py` работают на
`Qwen3.6-35B-A3B` (`llm.QWEN_35B`) с таймаутом одного вызова
`LAKE_RESEARCH_LLM_TIMEOUT_S=90` по умолчанию. Механические parse/generalize/
retrieve-rewrite шаги остаются на 9B; существующие link/trust judgement уже
используют 35B. Это отдельная модельная роль, а не изменение ранжирования или
схемы API.

RAG-приоры не копируются в task-local memory и не считаются доказательством.
Отсутствие RAG явно помечается `rag_status=empty|degraded`; отсутствие web
evidence при сломанном RAG даёт `503`. Модель не выносит verdict о feasibility,
fitness, usefulness или promotion — это ответственность Core и evaluator.

Совместимая копия агента остаётся в `gigaevo-core-runtime/` для старых запусков.
Активная интеграционная ветка Core вызывает `/research` в фоне; его
`EvolutionResearchAgent` формирует и выбирает task-local hypotheses из отчёта,
а не инжектирует карточки Lake напрямую.

```
POST /retrieve
  { query, k=5, run_id?, budget?, rewrite=true, allow_web=false }
->
  { ideas: [ { idea_id, text, applicability_conditions, limitations, failure_modes,
               effect_claimed, effect_observed, trust_score, score, cosine_similarity, via,
               theses: [ { text, url, title, effect, locator } ] } ],
    log_id, cost: { tokens_in, tokens_out, wall_ms } }

POST /research
  { query, context?, known_ideas?, directions?, max_queries?, max_sources?, rag_k?, run_id? }
->
  { report, queries, sources: [ { source_id, title, url, excerpt } ],
    rag_status, rag_log_id, rag_ideas, warnings,
    cost: { tokens_in, tokens_out, wall_ms } }
```

400 — битый JSON, нет `query`, `k <= 0`, `budget <= 0`, `allow_web=true`, неизвестное поле
(`extra="forbid"`: опечатка в имени поля обязана падать, а не игнорироваться). Не 422:
C интегрировался на 400, тело осталось `{"error": ...}`.
503 — хранилище недоступно или упало. Это не то же самое, что пустая выдача: `ideas: []` с живым
графом — данные для замера «с озером против без», а упавший граф — не данные вообще. `--mock`
отдаёт ту же форму с захардкоженными данными и не трогает ни граф, ни LLM, и **не пишет строку в
лог выдачи** — мок в метриках это загрязнение. Мок влияет только на `/retrieve`, остальные ручки
работают как обычно.

Ручки объявлены обычным `def`, не `async def`: ранжирование блокирующее (sqlite, numpy, один вызов
LLM на переписывание), поэтому Starlette уводит её в свой threadpool и не занимает event loop.
`contextvars` туда копируются — на этом держится по-запросный `cost` (см. `trace.request`).

### 5.2 Python — публичные функции

```python
# lake/llm.py — один вызов, принуждённый схемой; отклонение = LLMError, не пустая строка
complete(prompt, *, system, schema, op, max_tokens, timeout, model=QWEN_9B, temperature=0.0) -> dict
assert_grammar_works(model) -> None        # канарейка, гоняется в начале каждого прогона
load_prompt(step) -> str

# lake/embed.py — модель грузится один раз на модуль, лениво
embed_docs(texts) -> np.ndarray            # (n, 384), L2-normalized
embed_query(text) -> np.ndarray            # (384,), с query-префиксом

# lake/index.py — индекс тезисов, data/index.db, переживает переезд на Neo4j
index_theses(theses, db=INDEX_DB) -> None
search_theses(query, k, query_vec=None, db=INDEX_DB) -> list[dict]
reset(db) -> None ; index_rows(rows, db) -> int ; count(db) -> int ; rebuild_from(path, db) -> int
reconcile(rows, db=INDEX_DB) -> int        # путь §6.19: проверить всё, ПОТОМ сносить и собирать

# lake/graph_client.py — единственное место, знающее формат B
write_source(src) -> str
write_theses(source_id, theses) -> list[str]
create_idea(idea) -> str
create_idea_with_theses(idea | None, source_id, theses) -> list[str]   # одна транзакция
update_idea(idea_id, fields) -> None
split_idea(parent_id, parent_fields, children) -> None   # одна транзакция, issue #2
get_ideas(ids) -> list[dict]               # листья уже склеены с source.type/url/title
get_leaves(idea_id) -> list[dict] ; leaf_count(idea_id) -> int
neighbors(ids, hops=1, min_weight=None) -> list[dict]
all_theses() -> list[dict] ; ideas_without_leaves() -> list[str] ; trust_scale() -> float
dirty_ideas(limit=None) -> list[str]       # dirty=1, старейшие первыми (`13` §3.2)
set_trust(idea_id, score) -> None          # ЕДИНСТВЕННОЕ место, где dirty снимается
backend_name() -> "neo4j"                  # для лога и /healthz (D11, 2026-07-31)
get_source(id) ; list_sources(limit, offset) ; list_idea_ids(limit, offset)
list_theses(idea_id, source_id, limit, offset) ; count_theses(idea_id, source_id) ; get_thesis(id)
counts() -> {source, idea, thesis, edge}   # постраничное чтение для HTTP-слоя
# [D11, 2026-07-31] NEO4J_URI обязателен, SQLite-бэкенд удалён целиком. Отказ на старте,
# если переменная не задана (новая конфигурация, забыли переменную = падение, не молча).
# NEO4J_URI (умолч. "bolt://localhost:7687" для локальной разработки, проверяется при импорте),
# NEO4J_USERNAME и NEO4J_PASSWORD (умолч. отсутствуют — контейнер поднят с NEO4J_AUTH=none),
# NEO4J_DATABASE (умолч. "neo4j") — это ЧТЕНИЕ (локальное озеро). neo4j_load.py --wipe/push
# в Aura пишет через ОТДЕЛЬНЫЕ NEO4J_TARGET_URI/USERNAME/PASSWORD/DATABASE (BLOCKER
# третьего раунда: одна переменная на чтение и запись позволяла залить граф сам в себя).
# update_thesis НЕТ и не будет: иммутабельность тезиса держится отсутствием метода (§3.4).
# `split_idea` — не он: §1.2 про то, что сказал ИСТОЧНИК (text, context, effect, locator,
# text_hash, source_id), а `idea_id` — решение арбитра фазы 2, и оно обязано быть чинибельным,
# иначе единственный ремонт неверной линковки — удалить лист. Это единственный во всём блоке A
# `UPDATE` по таблице `thesis`, он пишет ровно одну колонку, и §6.9 проверяет ВЕСЬ список SET,
# а не первую колонку: `SET idea_id=?, text=?` красит проверку. `INSERT OR REPLACE` по `thesis`
# отказывает сам `_insert` — такой перезаписи никакой `UPDATE` в исходнике не видно.

# write path
ingest.fetch.fetch_source(entry) -> (Source, list[Section])
ingest.fetch.arxiv_id_from_url(url) -> str             # ссылка arXiv → id, иначе FetchError
ingest.parse.parse_section(section, abstract, limitations) -> list[DraftThesis]
ingest.parse.parse_document(sections, abstract, limitations) -> (list[DraftThesis], report)
ingest.generalize.generalize(draft) -> IdeaFields
ingest.generalize.leakage(draft, out) -> list[str]     # пусто = утечки конкретики нет
ingest.link.link_batch(source_id, rows) -> list[dict]
ingest.rederive.maybe_rederive(idea_id) -> bool
ingest.rederive.derive(idea, leaves) -> dict          # шесть полей §4.6, ничего не пишет; dirty не трогает (`13` §3.2)
ingest.split.due(max_leaves=16) -> list[str] ; ingest.split.split_idea(idea_id) -> dict
ingest.split.leaf_counts() -> dict[idea_id, int]      # распределение листьев, максимум в отчёт
ingest.trust.judge(idea, leaves) -> dict              # 35B: {score 0..1, reason, leaves_shown, leaves_total}; raises на отказе
ingest.trust.refresh(idea_id) -> dict                 # judge + graph_client.set_trust; идея без листьев -> 0.0, судью не зовёт
ingest.trust.sweep(idea_ids) -> dict                  # никогда не raises; trust_scored/trust_failed/trust_errors/trust_leaves_capped/trust_mean
ingest.trust.MAX_LEAVES = 16 ; ingest.trust.leaf_order(leaves) -> list[dict]  # срез детерминирован: run с исходом первыми
ingest.runlog.payload_from_csv(path, run_id=None) -> dict     # CSV -> тело POST /run
ingest.runlog.from_payload(payload, staging_path=None, *, limit=None, min_abs_delta=0.0) -> dict  # -> отчёт (§9 п.3), не int
ingest.runlog.from_csv(path, *, limit=None, min_abs_delta=0.0, staging_path=None, run_id=None) -> dict
ingest.runlog.drain_run(staging_path, staged=None) -> dict    # фаза 2 одного батча логов, поверх run.phase2
ingest.runlog.leak_terms(source) -> tuple[str, ...]   # список запретов для утечки на логе (`13` §2.2.1)
ingest.run.phase1(entries, workers=8) -> int ; ingest.run.phase2(staging_path, limit=None) -> dict
ingest.run.stage_one(entry, staging_path) -> dict      # фаза 1 одного источника, граф не трогает
ingest.run.drain_one(staging_path, staged=None) -> dict  # фаза 2 + два стража «неизменившееся озеро — не успех»
ingest.run.ingest_one(entry, staging_path) -> dict     # = stage_one + drain_one; /fetch синхронно, CLI, selfcheck
ingest.run.TRUST_PER_PASS = int(os.environ["LAKE_TRUST_PER_PASS"] or 50)  # потолок судьи за один проход фазы 2

# read path
retrieve.rewrite.rewrite(query, budget=None) -> (query, failed)
retrieve.search.search(query, query_vec, top_k=50, fuse="rrf"|"minmax") -> list[dict]
retrieve.rank.rank(query, k=5, query_vec=None) -> (ideas, log_payload)
retrieve.api.retrieve(query, k=5, ...) -> (status, body)   # транспорт-независимое ядро

# составные операции — то же самое, что делают ручки, но без единого знания про HTTP
ops.upsert_source(url, title, type, version="v1", ...) -> dict   # Conflict, если едет title/type
ops.patch_idea(idea_id, fields) -> dict                          # text тянет за собой вектор
ops.reindex() -> dict ; ops.stats() -> dict ; ops.health() -> dict
ops.staging_state() -> dict ; ops.pending_link(limit=50) -> list  # Broken на битых файлах
# исключения: OpsError -> NotFound (404) | Conflict (409) | Broken (503)

# HTTP-слой
api.app.create_app(mock=False, warmup=True, workers=True) -> FastAPI ; api.app.app
api.jobs.start(kind, fn, args) -> dict     # фоновое задание, слот один; занят → Busy
api.jobs.exclusive(kind) -> ctx            # тот же слот для короткой работы внутри запроса

# lake/queue.py — долговечная очередь /fetch, data/jobs.db, переживает рестарт процесса
queue.enqueue(kind, args, *, dedup_key=None, ceiling=0, db=None) -> dict   # Full, если ceiling достигнут
queue.claim(status, stage, *, db=None) -> dict | None       # атомарный UPDATE ... RETURNING
queue.stage(job_id, report=None, *, db=None) -> None ; queue.release(job_id, back_to, *, db=None) -> None
queue.finish(job_id, status, *, report=None, error=None, db=None) -> None
queue.retry(job_id, back_to, error, *, db=None) -> dict | None   # MAX_ATTEMPTS=3, дальше failed
queue.recover(*, db=None) -> dict          # рестарт: running -> статус, с которого забрали
queue.get / queue.listing / queue.counts / queue.close()
queue.MAX_ATTEMPTS = 3 ; queue.KEEP_FINISHED = 200 ; queue.DB = models.JOBS_DB   # перепривязываемая

# lake/writer_lock.py — один писатель фазы 2 на озеро, поверх процессов (§4.5)
writer_lock.held() -> ctx                  # реентерабельно; SecondWriter, если замок у другого процесса
writer_lock.acquire() / release() / depth() ; writer_lock.LOCK_PATH = DATA / "writer.lock"

# lake/api/workers.py — пул фазы 1 + единственный писатель фазы 2 поверх queue.py
workers.fetch_step() -> bool ; workers.write_step() -> bool     # один шаг, гоняются циклом
workers.start(*, fetch_workers=None, writer=True) -> dict       # flock, затем queue.recover(), затем потоки
workers.stop(timeout=5.0) -> None ; workers.alive() -> dict     # какие потоки живы (для /healthz)
workers.FETCH_WORKERS = int(os.environ["LAKE_FETCH_WORKERS"] or 2)
workers.QUEUE_MAX = int(os.environ["LAKE_QUEUE_MAX"] or 100)
workers.POLL_S = float(os.environ["LAKE_QUEUE_POLL_S"] or 1.0)
```

Границы, которые держатся кодом, а не аккуратностью:
формат B знает только `graph_client.py`; модель эмбеддингов и query-префикс не покидают блок A;
`effect_claimed` и `effect_observed` — два отдельных свойства схемы, поэтому не сливаются грамматикой.

---

## 6. Fail-closed: что ловится ассертом, а не глазами

Ни одна из этих поломок не выдаёт себя исключением — каждая даёт HTTP 200 и правдоподобный ответ.

| Тихая поломка | Где закрыта |
|---|---|
| упор в `max_tokens`, JSON обрывается на полуслове | `finish_reason != "stop"` **до** `json.loads` (`llm.py`, §3.1 п.5) |
| грамматика обрезала строку посреди слова, `finish_reason="stop"` | длина строки == `maxLength` в схеме → `LLMError` (§3.1 п.7) |
| зависший сокет вешает 25-минутную фазу | `timeout` обязателен, без дефолта (§3.1 п.4) |
| сервер молча игнорирует схему | канарейка на каждой модели в начале прогона |
| пустой FTS-индекс, гибрид вырождается в косинус | обычная `fts5`, наполняется явным INSERT + проверка 6.12 |
| неявный AND в FTS5 | `fts_escape()` + проверка 6.13 |
| сбой арбитра → «новая идея», дубли копятся | `pending_link`, тезис не пишется (проверка 6.8) |
| идея без листьев после отказа на записи | одна транзакция (проверка 6.17) |
| индекс разъехался с графом | индексация в том же шаге + проверка 6.19 |
| `PATCH` с явным `null` пишет NULL в ненулевую колонку — строка больше не читается, и с ней падают `/ideas` и весь `/retrieve` | `null` запрещён в `IdeaPatch` **и** в `update_idea`: страж в хранилище закрывает и будущих вызывающих |
| пересборка индекса упала после сноса → индекс пуст, а пустой индекс не бросает: `/search` отдаёт `200 []`, ранжирование читает это как «в озере ничего нет» | снос, создание и заливка одной транзакцией (`index._rebuild`), векторы проверены до неё |
| задание держит единственный слот навсегда, если поток не стартовал, и врёт статусом `running` | слот отпускается в `except BaseException` вокруг `Thread.start` |
| ответ не прошёл валидацию модели → `500`, а в логе выдачи уже записан успех с двумя идеями | тело валидируется в `retrieve.api` **до** записи строки лога |
| «лист» считается по-разному: `/theses` через JOIN, а `/stats` без него → `in_sync: false` навечно по строкам, которых никто не видит | одно определение: `counts`, `all_theses`, `ideas_without_leaves` идут тем же JOIN |
| курсор ушёл за конец staging → `pending_lines: 0`, «всё загружено» | `503` с числами; мусор в курсоре и рваная строка staging тоже названы поимённо |
| сбой посреди выгрузки оставляет папку без README и с половиной заметок — в Obsidian это открывается не как поломка, а как озеро поменьше | сборка в `.export.tmp`, счёт файлов там же, и только потом подмена; id проверяются до удаления старого |
| `--dest` по опечатке стирает чужие заметки: скан чужаков смотрел имена верхнего уровня, а `rmtree` рекурсивный | маркер `.lake-vault`: каталог опознаётся по факту, а не по совпадению имён |
| `[[` в цитате из статьи становится ссылкой, граф обрастает узлами, которых в озере нет; при `[[[a]]]` двойная замена сама рождает новую пару | экранируется каждая скобка, поэтому двух соседних неэкранированных не бывает; `WIKILINK_RE` ослаблен, иначе проверка этот случай не видит |
| занятый слот отдаёт `500`, хотя OpenAPI обещает `409`: `jobs.Busy` — голый `RuntimeError`, и две ручки конвертировали его руками, а третья забыла | обработчик на `jobs.Busy` в `app.py` — забыть его нельзя |
| обязательное поле модели не пришло от читателя — узел уезжает в Neo4j с дырой и молча: схемы там нет, возразить некому (так 60 тезисов уехали без `vector`) | `_row` требует каждое обязательное поле; правило «`None` → не писать» осталось только для полей, объявленных `\| None` |
| второй процесс делает `queue.recover()` до захвата замка: живые `running`-задания держателя замка возвращаются в очередь и гоняются дважды, а сам процесс потом получает отказ | `flock` берётся первым, `recover()` — только у владельца замка (проверка 6.25) |
| `queue.finish()` вне охраняемого блока: статья в графе, строка навсегда `running`, после рестарта — `failed` на успешной работе | терминальный переход внутри `try`; сбой самого `finish` уходит в повтор, а повтор фазы 2 идемпотентен |
| `stop()` отпускает замок по таймауту join (секунды) против фазы 2 (минуты) → новый контейнер поднимает **второго** писателя | замок держится, пока `writer` жив; живые потоки остаются в `alive()` |
| фазу 2 запускают мимо `workers.py` — CLI `run.phase2` с хоста или `/ingest/phase2` во втором процессе: `jobs.exclusive` этого не видит | замок берётся внутри `run.phase2`, то есть на каждом входе (проверка 6.24) |
| писатель получил `staging`-файл и вернул отчёт без чисел фазы 1: `theses_dropped` теряется, `cost` занижен на fetch/parse/generalize — а это метрика блока D | отчёт фазы 1 едет на строке задания и сливается в `write_step` |
| пустой (или исчезнувший) `staging`-файл проходит фазу 2 нулями и заканчивается `ok` | `drain_one` считает строки с диска и отказывает на нуле |
| арбитр отказал на всех тезисах → повтор: первая попытка честно падает, но курсор уже прошёл группу, вторая обрабатывает ноль групп, все счётчики нули — и это читалось как чистый реплей: `ok`, staging удалён, статьи в графе нет | два стража, а не один: счётчики этого прогона **и** хранилище (`count_theses(source_id=...)` — нет ни одного листа, значит ингеста не было, что бы ни говорили счётчики и курсор) |
| `/fetch` в `--mock` принимает задание и пишет в настоящий `data/jobs.db`, хотя `/healthz` обещает «no store touched» | `503` с причиной; ни одно задание не заводится |
| `/fetch` и `/ingest/jobs*` теперь зависят от `jobs.db`, а `503` не было в их OpenAPI | статус описан на всех трёх ручках |
| судья доверия молча возвращает `0.0` на отказе (пустой ответ, счёт вне `enum`, упавший вызов) — а `0.0` в этом поле легален и означает «оценили и не доверяем» | `trust.judge` `raise`ит на счёте вне `SCORES` (`ingest/trust.py`); `refresh`/`sweep` при отказе не пишут ничего — старый счёт и `dirty=True` остаются, отказ уходит в `trust_failed`/`trust_errors`, никогда не в `trust_score` |
| `dirty` снимается вторым писателем помимо `set_trust` (например, пере-выводом заодно с шестью полями §4.6) — тогда идея может стать «чистой» со старым счётом доверия | `rederive.py` явно не трогает `dirty` (см. шапку модуля); единственный `UPDATE`, который его снимает, — `graph_client.set_trust`, тем же вызовом, что пишет счёт |
| судье на вход идёт вся идея целиком — на идее с 92 листьями (§8 п.3) это упор в контекст и `LLMError` ровно там, где судья нужнее всего | потолок 16 листьев, детерминированный отбор (сначала `run`-листья с исходом, потом по `created_at`); оба числа — `leaves_shown`/`leaves_total` — в трейсе и в ответе (`ingest/trust.py`) |
| свод судьи без потолка — на большом озере один проход фазы 2 повесится на сотнях вызовов 35B за одним `/fetch` | `LAKE_TRUST_PER_PASS` (умолч. 50); остаток не потерян — остаётся `dirty` и виден числом `trust_deferred`, а не тонет в «doing my best» |
| `-1000.0` в логе эволюции читается как настоящий провальный фитнес и утаскивает формулу/дельту в минус тысячу на одном мутанте | `DEAD_FITNESS = -1000.0` в `runlog.py`: не участвует ни в `run_success`, ни в `effect`, ни в дельте — отдельный счётчик `dropped_dead` |
| **[D11, 2026-07-31]** забытый `NEO4J_URI` (дефолта нет) — отказ на старте с явной ошибкой (`graph_client.py:91`) | отказ на старте, не тихое падение в графе. `sqlite3.Error` не читается как ошибка графа из-за работы индекса и очереди отдельно |
| батч логов эволюции, где ни у одного мутанта нет `changes[]`, отчитывается `ok` с нулями вместо `failed` | `from_payload` `raise`ит с одним из трёх разных сообщений (всё умерло до измерения / измерено, но `changes[]` нет / всё срезано `limit`/`min_abs_delta`) — пустой отчёт никогда не возвращается |
| в батче логов первый мутант отказан арбитром целиком, а остальные легли в граф — `drain_one`-стиль проверки по первому источнику решил бы, что это провал | `drain_run` проверяет наличие листа по **каждому** `source_id` батча, а не только по первому (`runlog.py`) |
| `score` — min-max по кандидатам ЭТОГО вызова: лучший элемент всегда `1.0`, у запроса про закваску для хлеба (в озере нет ничего похожего) ровно как у релевантного — звонящий не может отличить «нашли» от «ничего не нашли и это лучшее из худшего» (`13` §7, ревью 2026-07-31) | аддитивное поле `cosine_similarity` (`rank.py`) — косинус запрос·идея, не перенормируется по вызову; самопроверка ловит красным, если оно перестаёт различать два разных запроса (`rank.demo`, §8 п.1) |

`python3 -m lake.selfcheck` — на 2026-07-31 файл регистрирует **25** проверок
(`CHECKS`, `@check(1..25)`), это те же 19 из `10:§6` плюс более ранние правки; прогон
`python3 -B -m lake.selfcheck --offline` в этот момент даёт **15/25 ok, 1 skipped (6.1,
сеть), 9 FAILED** (6.5, 6.6, 6.7, 6.10, 6.12, 6.13, 6.18, 6.19, 6.20) — семь из девяти
падают одной причиной: общая фикстура `_corpus()` не ждёт вызовов `trust`, которые
теперь идут в конце фазы 2 (свод судьи, §3-4), и падает на первом же из них; 6.18 всё ещё
проверяет `dirty` по правилам той эпохи, когда поле было за B и не двигалось; 6.20 не
совпадает с шаблоном заметки идеи после того, как в неё добавили `trust_score` и
`rederived_at_leaf_count`. Это фиксация одного момента, не гарантия на будущее: другие
части блока A правятся параллельно с этим файлом, и оба числа — 25 всего и 15 зелёных —
могут быть уже другими к моменту чтения; перепроверяется командой из §1. Мутационное
происхождение самих 25 проверок никуда не делось: сломай любую из защит выше их
собственной эпохи, и краснеет ровно её пункт. Очередь и писатель прогнаны отдельным кругом
— 19 дефектов по одному, 15 покраснели сразу, а 4 вскрыли дыры в самих проверках, и дыры
важнее находок: проверка гонки `claim` заполняла очередь **до** старта процессов, поэтому
первый успевал забрать все строки до того, как второй импортировал модуль (замер: `[0,
60, 0, 0]`, зелено с racy `claim`) — теперь процессы синхронизируются по «ready» и
стартуют на полной очереди, и однопроцессный прогон назван ошибкой, а не принят молча;
отказ соседнему процессу в `writer_lock` имитировался своим `flock`, поэтому не видел, как
`acquire()` открывает файл, — теперь сосед вызывает настоящий `acquire()`; ожидание в
проверке 6.25 сходилось и на фазе 1, из-за чего она краснела примерно в половине прогонов
на исправном коде (проверка, красная по своим причинам, не говорит ничего о страже, на
который наведена) — теперь ждём по стадии.

`python3 -m lake.api.selfcheck` — HTTP-слой, офлайн. Тоже прогнан на мутациях: 12 + 6 внесённых
по очереди и писателю (`/fetch` в `--mock`, срез `[:limit]`, `queue.listing()` со своим дефолтом,
дедуп по id в `/ingest/jobs`, ветка мёртвого пула в `/healthz`, отчёт фазы 1 мимо `drain_one`) и 12 из 12 прежних
по одному дефектов роняют проверку (снос индекса до валидации, `counts` без JOIN, `null` в патче,
обработчик `HTTPException` на подклассе FastAPI, `listing()` в обратном порядке, `/search` мимо `k`,
непереадресованный `TRACES_DIR` и т.д.). Заканчивается сверкой всего дерева `data/` по (размер,
mtime) до и после — сверка стоит в `finally`, иначе упавший ассерт уносил её с собой, и прогон,
который натёк, ровно тем и был прогоном, который отлаживают.

---

## 7. Что прогнано вживую

Канарейка 9B/35B → фаза 1 с `--limit 3`, в озеро доехали **2 источника** (60 тезисов, ровно
по 30 с каждого: оба упёрлись в потолок 30/документ, утечка конкретики 0/60) → фаза 2
(26 идей, `pending_link` пуст, идей без листьев 0, 3 мин 20 с) →
`/retrieve` (1.1–1.3 с при бюджете 5 с p95).

HTTP-слой прогнан на этих же данных: `/healthz` и `/stats` (2 источника, 26 идей, 60 тезисов,
`in_sync: true`), страницы `/sources` `/ideas` `/theses` с фильтрами, `404` в форме `{"error"}`,
`400` на `limit=0`, `409` на попытку сменить `type` существующего источника (записи не было),
`/search` (`bm25_rank` заполнен — FTS-плечо живо), `/retrieve` с переписыванием на 9B
(498 in / 20 out, 1.35 с), `POST /admin/reindex` (60 → 60, `in_sync` держится). OpenAPI: 22
операции, 12 объявляют `400`, 5 — `409`, 16 — `503`, ноль — `422`.

Выгрузка в Obsidian прогнана на тех же данных: 26 идей + 60 тезисов + 2 источника → 88 заметок
+ README, 240 ссылок `[[…]]`, битых 0, сирот 0, узлов старой схемы имён не осталось. Отпечаток
`lake/data/` до и после: вне `vault/` не изменился ни один файл, новый — только трейс чтений.
Граф открыт в Obsidian и просмотрен глазами: два источника — хабы, тезисы — лучи, идеи по краю,
подписи читаются. Пере-экспорт в каталог без маркера `.lake-vault` отказал с `Conflict` и текстом,
что делать, — то есть страж проверен не только на фикстуре.

Не прогнано: корпус целиком (84 источника), PDF-ветка (PyMuPDF не установлен), Neo4j (работает stub).

---

## 8. Известные ограничения

1. **`raw_score` при RRF почти не зависит от запроса.** Заведомо отсутствующий в озере запрос дал
   0.0305, релевантный — 0.0323. RRF считает по рангам, абсолютного качества в нём нет. Кривая
   «что теряли бы при пороге X» (§5.5) и третья группа запросов (§7) на этом плече не строятся —
   нужно плечо min-max (`search(..., fuse="minmax")`) или отдельное поле сырого косинуса.
   **[закрыто 2026-07-31, ревью «нет абсолютного сигнала релевантности»]** `score` страдает тем
   же: min-max по списку кандидатов ЭТОГО вызова, поэтому лучший элемент — всегда 1.0, каким бы ни
   был запрос (живой прогон: запрос про закваску для хлеба, которого в озере нет вовсе, вернул
   `score: 1.0` наравне с релевантным). Ни `score`, ни `raw_score` не годятся звонящему, которому
   надо решить «этого достаточно, или идти в интернет» (`13` §7). Добавлено аддитивное поле
   `cosine_similarity` (`lake/retrieve/rank.py`, `lake/api/schemas.py`, контракт C3
   `knowledge/07-roles-and-contracts.md`) — косинус между вектором запроса и **собственным**
   вектором идеи (`text` → vector, §1.3), не min-max плечо и не сырой RRF: он не перенормируется
   по вызову и потому сравним между запросами. Живой прогон (127.0.0.1:8077, 43 тезиса): лучший
   кандидат на "sourdough bread fermentation temperature and hydration ratio" — 0.44–0.48; на
   "population evolutionary search math reasoning" — 0.75–0.76. Не порог: у общего энкодера
   косинус между несвязанными текстами не стремится к нулю (анизотропия), так что 0 — не базовая
   линия отсутствия, а звонящий сравнивает число со своей измеренной базой. Самопроверка —
   `rank.demo()` (`lake/retrieve/rank.py`, п.5b): запрос в терминах одной фикстурной идеи
   ("far") против другой ("sharp") обязан менять, какая идея получает высокий конец шкалы —
   красная проверка, если сигнал перестал различать два запроса, поймана мутационно (см. §6).
2. **Арбитр переклеивает.** На первом прогоне у одной идеи 14 листьев, часть из них — результаты,
   а не приёмы. Ломается и правило 1 парсера, и гранулярность арбитра. Лечится приёмкой и правкой промпта.
3. **Крен «богатые богатеют»** на отборе кандидатов (§4.5) — снят, но не бесплатно (issue #2).
   На прогоне из 10 источников он успел схлопнуть 34% озера в одну идею: 92 листа из 9 источников,
   текст расширился до исследовательской области, `effect_claimed` стал списком из 18 чисел от
   несвязанных задач. Три правки, и каждая закрывает своё звено петли:
   `link._first_per_idea` умножает лучший ранг идеи на число её попаданий в окно (идея, занявшая
   `n` из 30 мест, по одному объёму стоит на ранге ~30/n — эта цена теперь вычитается);
   правило 2 промпта арбитра отказывает кандидату, который называет область, а не приём;
   `ingest/split.py` разрезает идею, перевалившую за `MAX_LEAVES = 16`, по векторам листьев и
   пере-выводит каждую часть. Потолок — калибровочная ручка: настоящий приём, повторённый
   16+ статьями, будет разрезан лишний раз, и это дешевле обратной ошибки.
   Уже существующий узел на 92 листа этим не лечится задним числом: он режется при следующем
   `phase2` — свип идёт и на каждый источник, и один раз после цикла, поэтому прогон с уже
   вычерпанным staging (ноль источников) его тоже режет, а не отчитывается «всё чисто».
   В отчёте два независимых числа: `split_failed` считает ПОПЫТКИ, `ideas_over_ceiling` и
   `max_leaves_per_idea` читаются из стора. Путать их — и есть тот самый статус, который врёт.
   Не закреплено проверкой: уточняющие итерации k-means в `_bisect`. На любой фикстуре, где
   темы разделимы настолько, что можно утверждать ожидаемый разрез, его находит уже стартовая
   пара, и `_KMEANS_ITERS = 0` остаётся зелёным. Закреплены детерминизм, непустота обеих
   сторон, завершаемость и восстановление известных тем.
4. **`rebuild_from(staging)` из §3.5 невозможен**: `idea_id` назначается в фазе 2, в staging его нет.
   Путь реконсиляции — `index.reset()` + `index.index_rows(graph_client.all_theses())`.
5. **§4.1 врёт в одном числе**: evo_search = 26, не 27 (сумма 84 сходится, значит неверно слагаемое).
6. FunSearch (Nature) недостижим ни одним из трёх путей фетча — помечен `skip` с причиной.
7. **[D12, 2026-07-31] Рёбра `(:Idea)-[:RELATED]->(:Idea)` пишет A в пайплайне:** cocitation в фазе 2 после создания идей источника (`lake/ingest/run.py:274`, `graph_client.write_cocitation_edges`), и `derived_from` при синтезе гипотезы (`lake/idea_merger.py:265`, `write_derived_from_edges`). Возникнув, ребра используются на дозаполнении выдачи (`neighbors()`, `via="edge"` в `/retrieve`). До D12 таблица `edge` была пуста (рёбра были за B), теперь полна.
8. **`trust_score` считается судьёй (`13` §3.3), но не влияет на ранжирование.**
   `TRUST_WEIGHT = 0.0` в `lake/retrieve/rank.py` — решение, а не недосмотр: доверие
   раньше было формулой (`log(1 + число источников)`), теперь — few-shot оценка 35B, и
   поднимать вес одновременно с заменой формулы означало бы менять два фактора разом и
   не измерить ни один. Число едет в ответе `/retrieve`, участвует в формуле счёта с
   нулевым множителем, и есть отдельная самопроверка (`rank.demo`, п.4b), что при
   ненулевом весе оно реально двигает порядок — то есть механизм жив, просто выключен.
   **D14 (2026-07-31):** вместо веса — квота на СОСТАВ выдачи, не на формулу: не более
   `floor(0.2·k)` идей с `trust_score == 0` (гипотезы попадают под неё по определению,
   `13` §5). Recall-first сильнее квоты — если доверенных физически не хватает, выдача
   всё равно длины `k`, добор идёт недоверенными сверх квоты, и это видно в логе полями
   `trust_quota`/`untrusted_returned`/`untrusted_over_quota` (`rank.demo`, п.9-11).
9. **Судья не откалиброван боевым прогоном.** Few-shot промпт (`prompts/trust/system.txt`)
   несёт 8 отработанных примеров и явную шкалу 0-10, но живого прогона на корпусе — того
   же рода, что описан в §7 для остального пайплайна, — с судьёй в этом README не
   зафиксировано; известны только его собственная самопроверка (`python3 -m
   lake.ingest.trust`) и поведение на фикстурах `lake.ingest.run.selfcheck`.
10. **`python3 -m lake.selfcheck` регрессировал вслед за судьёй.** На 2026-07-31 из 25
    зарегистрированных проверок 9 падают `--offline` (детали и точный список — конец §6):
    общая фикстура фазы 2 не ждёт вызовов `trust`, которые теперь идут в конце каждого
    прохода, и одна проверка (`6.18`) всё ещё читает `dirty` по правилам той эпохи, когда
    поле принадлежало B. Это не немой факт «где-то есть баг» — это открытая работа,
    которую эта правка документации не делает и не может делать (правки кода вне её
    мандата).

---

## 9. Чего ещё нет

`eval/queries.yaml` и `eval/score.py` (§7) — 30 запросов и метрики; нужны после ингеста корпуса.
Синтез гипотез (`Idea.origin="synthesized"`, синтетический лист §6 спеки 13) — форма готова,
пишет его блок B, отложено вместе с бюджетом. Разметка судьи доверия на боевом прогоне (см.
§8 п.9 выше). Заливка трёх реальных логов эволюции в озеро — конвертер и судья готовы и
проверены на фикстурах, сама заливка не выполнялась (`13` §12).
