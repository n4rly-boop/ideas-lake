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
| `lake/ingest/` | write path: fetch → parse → generalize → link → запись батчем |
| `lake/retrieve/` | read path: rewrite → гибридный поиск → подъём к идеям → ранжирование → `POST /retrieve` |
| `lake/index.py` | индекс тезисов: SQLite FTS5 + вектора + RRF |
| `lake/graph_client.py` | единственное место, знающее формат графового хранилища |
| `lake/prompts/` | промпты текстовыми файлами, не строками в коде |
| `lake/selfcheck.py` | assert-проверки инвариантов, один запуск |

Спека реализации и разборы прототипов-доноров — в локальной базе знаний
(`knowledge/`, в репозиторий не входит).

## Контракт наружу

```
POST /retrieve
  { query, k=5, run_id?, budget?, rewrite=true, allow_web=false }
->
  { ideas: [ { idea_id, text, applicability_conditions, limitations, failure_modes,
               effect_claimed, effect_observed, trust_score, score, via,
               theses: [ { text, url, title, effect, locator } ] } ],
    log_id, cost: { tokens_in, tokens_out, wall_ms } }
```

`via` — как идея попала в выдачу: `thesis` | `edge` | `padding`.

Политика recall-first: отказа по низкому скору нет, выдача дозаполняется до `k`, но всё
выданное и всё отсечённое пишется в лог со скорами. Хранилище недоступно → `503`, а не
пустой `ideas`: «в озере ничего нет» и «озеро сломано» — разные вещи для замера
«с озером против без».

## Запуск

Секреты — только из окружения, в репозиторий не попадают:

```
LAKE_KEY_9B      ключ к Qwen3.5-9B
LAKE_KEY_35B     ключ к Qwen3.6-35B-A3B
NEO4J_URI        neo4j+s://…
NEO4J_USERNAME
NEO4J_PASSWORD
NEO4J_DATABASE
```

```bash
python -m lake.selfcheck        # инварианты
python -m lake.ingest.run       # фаза 1 → data/staging.jsonl, фаза 2 → граф
python -m lake.retrieve.api     # POST /retrieve
```
