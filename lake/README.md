# Блок A — Ingestion + Retrieve

Реализация спеки `knowledge/10-implementation-spec.md` (локальная, в репозиторий не идёт).
Источник → тезис → идея на записи; запрос → выдача на чтении. Граф — блок B, эволюция — C, стоимость — D.

Зависимости: stdlib + `numpy`, `sentence-transformers`, `PyYAML`. HTTP — `urllib.request`.
Python 3.12. Платных API нет: LLM — серверы школы (llama.cpp), эмбеддинги — локально на CPU.

---

## 1. Точки входа

| Команда | Что делает |
|---|---|
| `python3 -m lake.ingest.run phase1 [--limit N] [--sources path]` | fetch → parse → generalize → `data/staging.jsonl`. 8 потоков. **В граф не пишет ничего** |
| `python3 -m lake.ingest.run phase2 [--limit N]` | staging → линковка → граф + индекс → пере-вывод. Последовательно, курсор |
| `python3 -m lake.ingest.run selfcheck` | офлайн end-to-end на фикстурах, временные БД |
| `python3 -m lake.retrieve.api [--port 8077] [--mock]` | HTTP-сервер `POST /retrieve` |
| `python3 -m lake.selfcheck [--offline]` | 19 проверок §6. `--offline` пропускает канарейку (единственный сетевой пункт) |
| `python3 tools/gen_sources.py` | `09-raw/a11-sources.yaml` → `lake/sources.yaml` (84 записи) |

Каждый модуль дополнительно исполняем: `python3 -m lake.index`, `python3 -m lake.embed`,
`python3 -m lake.ingest.link` и т.д. — свой `__main__` self-check без сети.

**Ключи** читаются из окружения в момент вызова, не на импорте: `LAKE_KEY_9B`, `LAKE_KEY_35B`.
Без них модули импортируются и 18 из 19 проверок проходят. Локально: `set -a; . ./.env; set +a`
(`.env` в `.gitignore`, ключи в репозиторий не попадают).

---

## 2. Раскладка

```
lake/
  models.py         # Source / Thesis / Idea + JSON-схемы для LLM + пути + хеши
  llm.py            # клиент llama.cpp: схема, канарейка, fail-closed
  embed.py          # snowflake-arctic-embed-s, 384d, на CPU
  trace.py          # C5: JSONL-трейс каждого вызова
  index.py          # индекс тезисов: FTS5 + numpy + RRF. Мой навсегда, на Neo4j не едет
  graph_client.py   # ЕДИНСТВЕННОЕ место, знающее формат B
  stub_store.py     # SQLite-бэкенд того же интерфейса — ВРЕМЕННЫЙ
  selfcheck.py      # 19 assert-проверок, один запуск
  sources.yaml      # сгенерирован маппером
  ingest/  fetch parse generalize link rederive run
  retrieve/ rewrite search rank api
  prompts/{parse,generalize,link,rederive,rewrite}/system.txt
  data/             # gitignored: raw/ cache/ traces/ logs/ staging.jsonl index.db lake.db
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
    → rederive идей, у которых len(leaves) - rederived_at_leaf_count >= 3
    → курсор
```

Порога косинуса на линковке **нет** — решает всегда арбитр, «дубля нет» говорит сентинелом `-1` (§0.6).
Батч-оверлей — условие корректности, а не оптимизация: без него тезис №2 не видит идею,
созданную тезисом №1, и одна статья заводит два дубля под один механизм (§0.1.13, `link.py`).

Отчёт фазы 2: источники, тезисы, идеи, доля идей с ≥2 источниками, длина `pending_link`,
доля утечек, **число идей без листьев (обязано быть 0)**, токены и время из трейсов.

---

## 4. Как работает чтение

```
POST /retrieve
  → rewrite (9B, 20 с): запрос «в терминах решения». Отказ НЕ фатален → сырой запрос + rewrite_failed
  → search: BM25 (FTS5) + косинус (numpy), слияние RRF k=60, top-50 тезисов
  → rank: thesis_id → idea_id → dedup по МАКСИМУМУ скора
           → raw_score сохраняется как есть
           → нормировка в [0,1] по ПОЛНОМУ списку кандидатов
           → score = norm_score + 0.15 · trust_norm (фиксированная шкала)
           → мало идей → neighbors(hops=1), via="edge" → дозаполнение, via="padding"
  → лог в data/logs/retrieve.jsonl: score, raw_score, via, cut_off, rewrite_failed, cost
```

Запрос обязан пройти `fts_escape()` перед `MATCH`: у FTS5 своя грамматика **и неявный AND**,
из-за которого 10-словный переписанный запрос вернул бы пустое BM25-плечо, а гибрид молча
выродился бы в чистый косинус (§5.2, `index.py:71`).

**Граница отказа.** Граф недоступен → **HTTP 503** `{error, log_id}`. Пустая выдача при живом
графе → **200** и `ideas: []`. Это данные для A/B, смешать их значит загрязнить главную метрику (§5.4).

---

## 5. Слой API

### 5.1 HTTP — контракт C3, единственная зависимость соседей от блока A

```
POST /retrieve
  { query, k=5, run_id?, budget?, rewrite=true, allow_web=false }
->
  { ideas: [ { idea_id, text, applicability_conditions, limitations, failure_modes,
               effect_claimed, effect_observed, trust_score, score, via,
               theses: [ { text, url, title, effect, locator } ] } ],
    log_id, cost: { tokens_in, tokens_out, wall_ms } }
```

400 — битый JSON, нет `query`, `k <= 0`, `allow_web=true` (стадия III в MVP не входит).
503 — граф недоступен. `--mock` отдаёт ту же форму с захардкоженными данными и не трогает ни граф, ни LLM.

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

# lake/graph_client.py — единственное место, знающее формат B
write_source(src) -> str
write_theses(source_id, theses) -> list[str]
create_idea(idea) -> str
create_idea_with_theses(idea | None, source_id, theses) -> list[str]   # одна транзакция
update_idea(idea_id, fields) -> None
get_ideas(ids) -> list[dict]               # листья уже склеены с source.type/url/title
get_leaves(idea_id) -> list[dict] ; leaf_count(idea_id) -> int
neighbors(ids, hops=1, min_weight=None) -> list[dict]
all_theses() -> list[dict] ; ideas_without_leaves() -> list[str] ; trust_scale() -> float
# update_thesis НЕТ и не будет: иммутабельность тезиса держится отсутствием метода (§3.4)

# write path
ingest.fetch.fetch_source(entry) -> (Source, list[Section])
ingest.parse.parse_section(section, abstract, limitations) -> list[DraftThesis]
ingest.parse.parse_document(sections, abstract, limitations) -> (list[DraftThesis], report)
ingest.generalize.generalize(draft) -> IdeaFields
ingest.generalize.leakage(draft, out) -> list[str]     # пусто = утечки конкретики нет
ingest.link.link_batch(source_id, rows) -> list[dict]
ingest.rederive.maybe_rederive(idea_id) -> bool
ingest.run.phase1(entries, workers=8) -> int ; ingest.run.phase2(staging_path, limit=None) -> dict

# read path
retrieve.rewrite.rewrite(query, budget=None) -> (query, failed)
retrieve.search.search(query, query_vec, top_k=50, fuse="rrf"|"minmax") -> list[dict]
retrieve.rank.rank(query, k=5, query_vec=None) -> (ideas, log_payload)
retrieve.api.retrieve(query, k=5, ...) -> dict ; retrieve.api.serve(port=8077, mock=False)
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

`python3 -m lake.selfcheck` — **19/19**, проверки прогнаны на мутациях: сломай любую из этих
защит, и краснеет ровно её пункт.

---

## 7. Что прогнано вживую

Канарейка 9B/35B → фаза 1 на 3 источниках (60 тезисов, утечка конкретики 0/60, 10 срезано
потолком 30/документ) → фаза 2 (26 идей, `pending_link` пуст, идей без листьев 0, 3 мин 20 с) →
`/retrieve` (1.1–1.3 с при бюджете 5 с p95).

Не прогнано: корпус целиком (84 источника), PDF-ветка (PyMuPDF не установлен), Neo4j (работает stub).

---

## 8. Известные ограничения

1. **`raw_score` при RRF почти не зависит от запроса.** Заведомо отсутствующий в озере запрос дал
   0.0305, релевантный — 0.0323. RRF считает по рангам, абсолютного качества в нём нет. Кривая
   «что теряли бы при пороге X» (§5.5) и третья группа запросов (§7) на этом плече не строятся —
   нужно плечо min-max (`search(..., fuse="minmax")`) или отдельное поле сырого косинуса.
2. **Арбитр переклеивает.** На первом прогоне у одной идеи 14 листьев, часть из них — результаты,
   а не приёмы. Ломается и правило 1 парсера, и гранулярность арбитра. Лечится приёмкой и правкой промпта.
3. **Крен «богатые богатеют»** на отборе кандидатов (§4.5): у идеи с 20 листьями двадцать шансов
   попасть в top-30, у идеи с одним — один. Измеряется, в MVP не устраняется.
4. **`rebuild_from(staging)` из §3.5 невозможен**: `idea_id` назначается в фазе 2, в staging его нет.
   Путь реконсиляции — `index.reset()` + `index.index_rows(graph_client.all_theses())`.
5. **§4.1 врёт в одном числе**: evo_search = 26, не 27 (сумма 84 сходится, значит неверно слагаемое).
6. FunSearch (Nature) недостижим ни одним из трёх путей фетча — помечен `skip` с причиной.
7. `differentiation` — `null` до появления рёбер у B. `trust_score` в stub — заглушка
   (логарифм числа различающихся источников), значение B её заменяет.

---

## 9. Чего ещё нет

`eval/queries.yaml` и `eval/score.py` (§7) — 30 запросов и метрики; нужны после ингеста корпуса.
Neo4j-ветка `graph_client`. Слой `run`-источников (контракт C4, ждёт логов прогонов от C).
