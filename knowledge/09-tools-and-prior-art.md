# 09 — Готовые инструменты и реализации для блока A

> Собрано 2026-07-27. Каталог того, что уже написано другими и что можно взять под шаги блока A (`08-plan-block-a.md`): ингест, извлечение, эмбеддинги, индекс, дедуп, ретрив, оценка, трейсинг.
> Метаданные репозиториев (звёзды, лицензия, последний push) — по `gh api` на эту дату. Полные разборы с адресами файлов и строк — в [`09-raw/`](./09-raw/).
> Вердикты: **БЕРУ** — ставится и используется; **РЕФЕРЕНС** — читаем код/промпт/схему, целиком не тянем; **МИМО** — не подходит или мёртв.
> Что не проверялось открытием страницы или кода — помечено «не проверено».

---

## 0. Сводка: десять пунктов блока A (`08:33-44`) → чем закрывается

| # | Шаг | Готовое | Пишем сами | Где |
|---|---|---|---|---|
| 1 | Список источников и загрузка | `arxiv.org/html/{id}` + ar5iv; PyMuPDF на статьях без HTML; корпус из 84 проверенных источников | `sources.yaml`, склейка, идемпотентность | §1, §11 |
| 2 | `1c` парсер: документ → тезисы | `response_format.json_schema` у llama.cpp; JSONL-вывод и валидация ответа (TrustGraph); схема полей ORKG; `qualifier` из SciClaim; MeasEval под число с единицей; таксономия ограничений LimitGen; few-shot карточки Nova; датасеты для проверки (SciREX, MeasEval) | промпт под нашу пятиполевую карточку | §2, §8 |
| 3 | `1d` генерализация и обезличивание | **ничего** — ближайшее (IG-Bench, Hope et al., деконтекстуализация Choi) не снимает привязку к воплощению | всё | §8.1, §8.3 |
| 4 | `2a` digest батчем по источнику | containment-провенанс TrustGraph: один объект провенанса на секцию вместо копии в каждом тезисе | батч и со-встречаемость | §2.3 |
| 5 | `2b-1` решение «линковать или создать» | протокол арбитра graphiti целиком (отбор кандидатов, энтропийный гейт, сентинел `-1`, EXACTLY N, отрицательный few-shot); батчевый вызов и нумерация кандидатов mem0; блокинг и метрики из entity resolution | каскад, калибровка, очередь `pending_link` | §5 |
| 6 | Инициатор пере-вывода идеи при смене листьев | ленивая политика LightRAG `operate.py:349-356` — пересборка описания по порогу накопления, а не на каждый лист | триггер и промпт пере-вывода | §6 |
| 7 | Retriever: поиск → подъём → dedup → ранжирование → обход рёбер | SQLite FTS5 (stdlib) + numpy; RRF `k=60`; эмбеддинги на CPU (`sentence-transformers`/`fastembed`); реранкер bge-reranker-v2-m3; Doc2Query и HyDE в LangChain/LlamaIndex; приёмы отсечения по распределению скоров | подъём тезис→идея, dedup по идеям, формула ранга, дозаполнение | §3, §4, §7 |
| 8 | `POST /retrieve` | `http.server.ThreadingHTTPServer` из stdlib | форма ответа C3 | §10 |
| 9 | Лог выдачи со скорами и отсечённым | **ничего целиком**; скелеты — `PruningStats` у `neo4j-graphrag`, счётчики провалов LightRAG `operate.py:1282-1283` | весь лог | §2.2, §6 |
| 10 | Трейсы токенов и времени | JSONL-декоратор ~20 строк; `usage.*` и `/tokenize` у llama.cpp | — | §10 |
| — | Метрики блока A | `pytrec_eval`, MiniCheck; методика рубрики G-Eval | рубрика actionability, метрика обезличивания | §9 |

**Соседние блоки, не мои** (`07:15-18`): схема хранения, Neo4j, создание рёбер, формулы веса и `trust_score` — B; парсинг логов прогонов — C; модель трудоёмкости — D. Найденное для них помечено ниже как «передать B» и в мой объём работ не входит.

---

## 1. Ингест: документ → секции

Разбор — [`09-raw/a1-ingest.md`](./09-raw/a1-ingest.md), рабочий скрипт — `09-raw/fetch_arxiv_sections.py` (проверен на живой статье).

| Инструмент | Лицензия | Живость | Что даёт | Вердикт |
|---|---|---|---|---|
| **`arxiv.org/html/{id}`** | — | live | готовые семантические секции (`ltx_section`, `ltx_abstract`, `ltx_bibliography`) без парсинга PDF | **БЕРУ** — основной путь |
| **ar5iv** `ar5iv.labs.arxiv.org/html/{id}` | — | live | HTML там, где основной путь даёт 404 (проверено на статье 1997 года) | **БЕРУ** |
| **PyMuPDF / pymupdf4llm** | **AGPL-3.0** (не MIT) | 10330★, релиз 2026-06-29 | самый дешёвый PDF→текст, без GPU/Java/Docker | **БЕРУ** — для статей без HTML |
| GROBID | Apache-2.0 | 5029★, push 2026-07-26 | лучшее структурирование PDF (TEI, секции, ссылки), CRF-режим 495 МБ, без GPU | РЕФЕРЕНС — Java/Docker-демон |
| docling (IBM) | MIT | 63.8k★, активен | топ-качество разбора, таблицы и формулы | РЕФЕРЕНС — тянет torch |
| MinerU | кастомная Apache-based (сменена с AGPLv3 в 2026) | 75.9k★ | ~20 ГБ зависимостей (vLLM/PaddleOCR/Ray) | МИМО |
| marker | оговорка на веса при выручке > $5M | 37.9k★ | дублирует docling | МИМО |
| nougat | — | последний push 2025-02 | — | МИМО, заброшен |
| papermage | — | push 2024-11, авторы пишут «unlikely to be maintaining» | — | МИМО |
| Semantic Scholar API | — | активен | метаданные, TLDR; лимиты в источниках противоречивы | РЕФЕРЕНС |
| OpenAlex | CC0 | активен; в 2026 введён usage-based pricing, $1/день бесплатно | метаданные, цитирования | РЕФЕРЕНС |
| Unpaywall | — | требует email | для arXiv бесполезен | МИМО |
| semchunk / langchain / llama-index splitters | — | — | чанкинг, которого не требуется при HTML-секциях | МИМО |

Ловушки: arXiv ToU — 1 запрос / 3 с (при сборе корпуса реально ловится 429); `gh api` врёт про лицензии (`NOASSERTION` у MinerU при чёткой формулировке в README) — читать LICENSE.

---

## 2. Structured output: чем принуждать модель к схеме

Разбор — [`09-raw/a2-structured-output.md`](./09-raw/a2-structured-output.md).

### 2.1 Механизмы llama.cpp (серверная сторона)

| Механизм | Где | Замечание |
|---|---|---|
| `response_format.json_schema.schema` | `/v1/chat/completions` | схема читается строго из этой вложенности (`tools/server/server-common.cpp:947-949`); плоская форма из README даёт пустую схему и свободный текст — открытый [PR #18963](https://github.com/ggml-org/llama.cpp/pull/18963) |
| `response_format.json_object` (+ `schema`) | там же | — |
| `tools` / `tool_choice` | там же | требует `--jinja`, включённого по умолчанию с [PR #17524](https://github.com/ggml-org/llama.cpp/pull/17524) |
| `grammar` (GBNF) | там же | собственный движок грамматик |

Ограничения, которые стоит знать заранее: `$ref` резолвится только после [PR #21699](https://github.com/ggml-org/llama.cpp/pull/21699) и упирается в `MAX_REPETITION_THRESHOLD 2000` ([issue #21228](https://github.com/ggml-org/llama.cpp/issues/21228)) — грамматика молча не собирается; `pydantic.model_json_schema()` всегда генерит `$ref`. Поддержаны `enum`, `const`, `anyOf`, `additionalProperties:false` (`common/json-schema-to-grammar.cpp:844-975`), `pattern` — частично. Tool-calls у Qwen3.5/3.6 разбираются PEG-автопарсером ([PR #18675](https://github.com/ggml-org/llama.cpp/pull/18675)); открытые баги: [#20837](https://github.com/ggml-org/llama.cpp/issues/20837), [#20182](https://github.com/ggml-org/llama.cpp/issues/20182), [#20198](https://github.com/ggml-org/llama.cpp/issues/20198).

### 2.2 Клиентские библиотеки

| Библиотека | Лицензия | Вердикт |
|---|---|---|
| instructor | MIT | МИМО — даёт `model_json_schema()` (тот самый `$ref`) и слепые ретраи |
| outlines, xgrammar, guidance | Apache-2.0 / MIT | МИМО — работают с логитами, через HTTP недоступны; у llama.cpp свой GBNF |
| pydantic-ai, LangChain structured output | MIT | МИМО — та же обёртка поверх схемы |
| **TrustGraph** `template/prompt_manager.py:143-209` | Apache-2.0 | **РЕФЕРЕНС** — единственный из проверенных, кто делает `json_schema` **плюс** серверную `jsonschema`-валидацию ответа |
| **`neo4j-graphrag`** `OnError.RAISE` + `PruningStats` | — (`NOASSERTION`, не проверено) | **РЕФЕРЕНС** — единственный из семи schema-guided систем с fail-closed по умолчанию; `PruningStats` — готовый скелет очереди отбракованного |

### 2.3 Приёмы формата вывода, которые стоит скопировать

- **JSONL вместо JSON-массива** — TrustGraph, `docs/tech-specs/jsonl-prompt-output.md:60-136`: обрыв генерации теряет одну строку, а не весь ответ; каждый объект валидируется отдельно.
- **Схема в промпте с `domain → range`** — TrustGraph `ontology-prompt.md`, 42 строки: явные типы у каждого свойства + правило «Only use properties defined above». Работает вместе с проверкой кодом после (`DEFAULT_VALIDATION_SCHEMA` в llama-index) — это два разных фильтра.
- **Цитата с проверяемым id** — KAG `reference_generator.py:46-52`: *«the cited symbol must exist in the id field of the references; otherwise, no citation should be provided»*. Галлюцинированная ссылка ловится одним `assert`.
- **Containment-провенанс** — TrustGraph `graph_rag.py`: *«One subgraph per chunk extraction, shared across all triples produced from that chunk»* — один объект провенанса на секцию вместо копии в каждом тезисе.

---

## 3. Эмбеддинги

Разбор — [`09-raw/a4-embeddings.md`](./09-raw/a4-embeddings.md).

| Модель | dim | ctx | Лицензия | Префикс | GGUF/CPU |
|---|---|---|---|---|---|
| **snowflake-arctic-embed-s** | 384 | 512 | Apache-2.0 | `"Represent this sentence for searching relevant passages: "` только на запрос | BERT, готовый GGUF |
| BGE-large-en-v1.5 | 1024 | 512 | MIT | тот же query-prefix | BERT → GGUF работает |
| gte-modernbert-base | 768 | 8192 | Apache-2.0 | не нужен | ModernBERT, поддержка в llama.cpp с PR #15641 |
| granite-embedding-small-r2 | 384 | 8192 | Apache-2.0 | не нужен | ModernBERT |
| Qwen3-Embedding-0.6B | ≤1024 | 32768 | Apache-2.0 | `Instruct: {task}\nQuery:{query}` | decoder, `--pooling last` |
| bge-m3 / multilingual-e5-large / arctic-embed-v2.0 | 1024 | 512–8192 | MIT / Apache-2.0 | e5 требует `query:`/`passage:` | **XLM-RoBERTa — llama.cpp не конвертирует** |

Находка по семейству: `arctic-embed-l-v2.0` — это `xlm-roberta`, `arctic-embed-m-v2.0` — архитектура `gte`, то есть **не то же самое, что v1**. Если понадобится русский или длинный контекст — брать `gte-modernbert-base` или `granite-embedding-small-r2`, а не arctic-v2.

**Чем считать без GPU:** `sentence-transformers` (уже стоит), `fastembed` (ONNX, без torch), ONNX Runtime + optimum, HF TEI, `model2vec` (на порядки быстрее, 85–95 % качества — только под абляцию). llama.cpp умеет отдавать `/v1/embeddings`, но серверу нужен флаг `--embeddings` при старте.

**Порог косинуса привязан к модели.** `semantic-router` (MIT) держит для трёх моделей одного вендора пороги **0.82 / 0.30 / 0.30** (`semantic_router/encoders/openai.py:19-33`), и ещё одно значение — для той же модели при усечённой размерности. Дефолты по другим энкодерам — 0.30–0.50. Теоретическая подкладка: Steck et al., arXiv:2403.05440 — косинус эмбеддингов может давать «arbitrary and therefore meaningless similarities».

---

## 4. Индекс и гибридный поиск

Разбор — [`09-raw/a3-hybrid-search.md`](./09-raw/a3-hybrid-search.md), рецепт — `09-raw/hybrid_recipe.py` (96 строк, self-check проходит).

| Инструмент | Лицензия | Живость | Вердикт |
|---|---|---|---|
| **SQLite FTS5** | public domain | stdlib (в Python 3.12 этой машины — sqlite 3.51.0, FTS5 есть, `bm25()` и porter-стемминг работают) | **БЕРУ** |
| bm25s | MIT | 1747★ | РЕФЕРЕНС — если понадобится контроль над вариантом BM25 |
| rank_bm25 | Apache-2.0 | релиз 2022 | МИМО |
| Whoosh / tantivy-py / Xapian | — | Whoosh не коммитится 2.5 года; у Xapian GPL и мёртвые биндинги | МИМО |
| **numpy brute-force cosine** | BSD | — | **БЕРУ** — на нашем масштабе индекс не нужен |
| faiss / hnswlib / usearch / Chroma / LanceDB / Qdrant | MIT/Apache | активны | МИМО на тысячах документов |
| sqlite-vec | Apache-2.0/MIT | сам себя называет «pre-v1, expect breaking changes» | МИМО |
| **RRF (Cormack et al. 2009), k=60** | — | дефолт Qdrant, Elastic, Neo4j | **БЕРУ** — не требует нормализации скоров |
| min-max + взвешенная сумма | — | так сделано в `gigaevo-memory`; Weaviate ушёл на relativeScoreFusion | РЕФЕРЕНС — второе плечо абляции |
| bge-reranker-v2-m3 | Apache-2.0 | активен | РЕФЕРЕНС — реранк top-50 на CPU |
| ColBERT/PLAID, MonoT5, LLM-as-reranker | — | — | МИМО — цена не окупается |
| **`neo4j-graphrag-python`** `HybridRetriever` | не проверено (`NOASSERTION`) | 1230★, push 2026-07-27 | **передать B** — взвешенный RRF поверх нативных vector + full-text индексов Neo4j 5.11+; закрывает их сторону контракта C2, у меня остаётся вызов |

---

## 5. Дедуп и связывание: что уже написано

Разбор — [`09-raw/a5-dedup.md`](./09-raw/a5-dedup.md), 1213 строк с адресами файлов.

### 5.1 Как это устроено в живых системах

| Система | Отбор кандидатов | Автоприём слияния | Решает |
|---|---|---|---|
| graphiti | cosine 0.6 (`node_operations.py:65`) | Jaccard 0.9 по 3-граммам **имени** (`dedup_helpers.py:34`), MinHash 32 перестановки | LLM (`node_operations.py:476`) |
| mem0 | top-10 без порога (`main.py:899`) | md5 (`main.py:990`) | LLM, батчем (`main.py:920-932`) |
| LightRAG | cosine 0.2 — это поиск (`constants.py:57`) | точное имя | — |
| cognee | — | `uuid5` от нормализованного имени (`DataPoint.py:160-176`) | — |
| A-mem | k=5 без порога (`amem.py:288`) | не сливает | LLM |
| txtai | `limit=15, minscore=0.1` (`graph/base.py:643`) | не сливает | — |

Общее: **ни одна не принимает решение о слиянии по порогу косинуса** — косинус везде только набирает кандидатов.

Отдельные приёмы, готовые к копированию:

- **энтропийный гейт** graphiti (`dedup_helpers.py:31-33`): короткие и низкоэнтропийные строки к нечёткому матчингу не допускаются, идут сразу к LLM;
- **нумерация кандидатов int** как анти-галлюцинация — mem0 (`main.py:918`, комментарий `# Map UUIDs to integers (anti-hallucination)`) и graphiti независимо;
- **сентинел `-1`** «дубля нет» — graphiti `NodeDuplicate.duplicate_candidate_id`;
- **форсированная кардинальность ответа** — «Your response MUST include EXACTLY N resolutions»;
- **отрицательный few-shot** — graphiti, «Java язык против Java острова»;
- системный сдвиг «не сливать» — *«NEVER fabricate … or mark distinct entities as duplicates»*.

Чего не копировать: md5 по сырому тексту без нормализации и сверка хэша только с top-10 (оба — mem0, `main.py:990`, `:976-981`); `temperature=0.7`, зашитая в клиенте KAG; fail-open с логом уровня INFO там же.

### 5.2 Entity resolution как готовая дисциплина

| Инструмент / работа | Лицензия | Что даёт | Вердикт |
|---|---|---|---|
| dedupe (dedupeio) | MIT | canopy-блокинг (`canopy_index.py:17`) — тот же «отбор кандидатов», известный 26 лет | РЕФЕРЕНС |
| splink | MIT | кластеризация связных компонент, метрики графа | РЕФЕРЕНС |
| py_entitymatching | BSD | `debug_blocker()` — целевой отбор пар на разметку вместо случайного | РЕФЕРЕНС |
| recordlinkage | BSD | классические метрики | РЕФЕРЕНС |
| B³ (Bagga & Baldwin, ACL P98-1012) | — | кластерная метрика рядом с парной | РЕФЕРЕНС |
| Gruenheid et al., PVLDB 2014 | — | измеренная цена инкрементального слияния без split/move: F1 .811 батч против .722/.754 | РЕФЕРЕНС |
| IR-book §17.2 | — | «порог + связные компоненты» = single-linkage, отсюда мегакластеры | РЕФЕРЕНС |
| datasketch (MinHash/LSH), SimHash, SemDeDup, text-dedup, NeMo Curator | MIT/Apache | лексический дедуп больших корпусов | МИМО — «одна мысль разными словами» лексического пересечения не имеет |

---

## 6. Системы агентной памяти и graph RAG

Разбор 26 репозиториев — [`09-raw/a6-memory-oss.md`](./09-raw/a6-memory-oss.md), 2545 строк. Метаданные `gh api` на 2026-07-27.

| Репозиторий | ★ | Лицензия | Push | Что можно взять | Вердикт |
|---|---|---|---|---|---|
| getzep/graphiti | 29254 | Apache-2.0 | 2026-07-27 | двухслойность `(:Episodic)-[:MENTIONS]->(:Entity)` с провенансом; весь протокол арбитра дедупа | **БЕРУ (промпты и протокол)** |
| mem0ai/mem0 | 61853 | Apache-2.0 | 2026-07-25 | батчевый вызов на решение, нумерация кандидатов | БЕРУ (приёмы) |
| HKUDS/LightRAG | 38239 | MIT | 2026-07-27 | **моё:** ленивая пересборка описания по порогу накопления (`operate.py:349-356`) — это шаг «пере-вывод идеи»; счётчики провалов в статусе операции (`operate.py:1282-1283`) — половина `pending_link`. **Передать B:** вес ребра = счётчик различающихся источников, считает код, не LLM (`operate.py:2713-2745`) | БЕРУ (пере-вывод) |
| trustgraph-ai/trustgraph | 2390 | Apache-2.0 | 2026-07-27 | JSONL-вывод, containment-провенанс, схема+валидация (§2.3) | БЕРУ (приёмы) |
| neo4j/neo4j-graphrag-python | 1230 | не проверено | 2026-07-27 | `HybridRetriever`, `OnError.RAISE`, `PruningStats` | БЕРУ |
| OpenSPG/KAG | 8936 | Apache-2.0 | 2026-01-28 | формат цитаты с проверяемым id; двухфазность NER→отношения | РЕФЕРЕНС (два куска) |
| microsoft/graphrag | 34912 | MIT | 2026-07-26 | многораундовый gleaning; `relationship_strength` 1–10 от LLM | РЕФЕРЕНС |
| topoteretes/cognee | 29451 | Apache-2.0 | 2026-07-27 | `uuid5`-нормализация имени | РЕФЕРЕНС; JSON просит словами — не копировать |
| OSU-NLP-Group/HippoRAG | 3892 | MIT | 2026-07-24 | двухфазное извлечение | РЕФЕРЕНС |
| letta-ai/letta | 23985 | Apache-2.0 | 2026-07-22 | архитектура памяти агента | РЕФЕРЕНС |
| Mirix-AI/MIRIX | 3559 | Apache-2.0 | 2026-07-25 | типизация видов памяти | РЕФЕРЕНС |
| infiniflow/ragflow | 86158 | Apache-2.0 | 2026-07-27 | парсинг документов | РЕФЕРЕНС |
| langchain-ai/langmem | 1583 | MIT | 2026-07-25 | — | РЕФЕРЕНС |
| AuvaLab/itext2kg | 954 | Apache-2.0 | 2026-04-30 | двухфазность | РЕФЕРЕНС |
| gusye1234/nano-graphrag | 3946 | MIT | 2026-01-27 | компактная реализация для чтения | РЕФЕРЕНС |
| memodb-io/memobase | 2794 | Apache-2.0 | 2026-01-11 | — | РЕФЕРЕНС |
| kingjulio8238/Memary | 2634 | MIT | 2024-10-22 | — | МИМО, мёртв |
| TencentCloudADP/youtu-graphrag, FalkorDB/GraphRAG-SDK | 1224 / 979 | не проверено / Apache-2.0 | 2026-02 / 2026-07 | ручной парсинг JSON | МИМО |

Два сквозных факта по семи schema-guided системам: **fail-closed по умолчанию только у `neo4j-graphrag`** (остальные выбрасывают невалидное молча — это моё, §2.2); **весов на рёбрах нет ни у одной** (GraphRAG-SDK объявляет `weight: float = 1.0` и не использует), а расхождение по способу их считать — graphiti весов не имеет вовсе и держит вместо них `valid_at`/`invalid_at`, LightRAG считает кодом, microsoft/graphrag просит число у LLM — **передать B**, к моему объёму не относится.

---

## 7. Переписывание и расширение запроса

Разбор с числами — [`09-raw/a7-query-rewrite.md`](./09-raw/a7-query-rewrite.md), 14 работ.

| Приём | Замеренный эффект | Реализации |
|---|---|---|
| **Doc2Query / docTTTTTquery** — генерировать при записи вопросы к документу и индексировать вместе с ним | MS MARCO MRR@10 0.184 → 0.218 → 0.277; Recall@1000 0.853 → 0.947 | оригинальный код авторов; тривиально воспроизводится одним LLM-вызовом на документ |
| **HyDE** — гипотетический ответ вместо запроса | до +32.0 nDCG@10 на TREC-COVID; **проигрывает** fine-tuned Contriever на FiQA и DBPedia | LangChain `HypotheticalDocumentEmbedder`, LlamaIndex `HyDEQueryTransform` |
| **query2doc** — конкатенация запроса с сгенерированным псевдодокументом | — | тривиально |
| Rewrite-Retrieve-Read, step-back, RAG-Fusion, multi-query | — | LangChain `MultiQueryRetriever`, LlamaIndex `StepDecomposeQueryTransform` |
| «Not All Queries Need Rewriting» (arXiv:2603.13301, июль 2026) | rewrite вредит на FiQA (−9.0 % nDCG), помогает на TREC-COVID (+5.1 %), нейтрален на SciFact; разделяющий фактор — лексическое совпадение домена | — |

Смежное, про формирование выдачи: «Power of Noise» (Cuconasu et al., SIGIR 2024) — случайные нерелевантные документы в контексте точность **улучшают** (до +35 %), а пограничные «похожие, но нерелевантные» **роняют** (до −67 % при 18 таких документах). Приёмы отсечения по распределению скоров: autocut (Weaviate), score-distribution cutoff (Arampatzis 2009), BiCut / Choppy / AttnCut.

---

## 8. Схемы карточек, онтологии, датасеты

Разбор — [`09-raw/a9-prior-art.md`](./09-raw/a9-prior-art.md).

### 8.1 Готовые схемы полей

**ORKG template `Statistical Method`** (R1905056, создан 2026-07-12, данные ORKG — **CC0 1.0**, `https://orkg.org/api/templates/R1905056`), 13 полей verbatim:

`method name` · `Category` · `Problem type` · `Input data type` · `Assumptions` · `Model parameters` · `Evaluation metrics` · `Reported performance` · `Dataset used` · `Tool or software used` · `Advantages` · `Limitations` · `Application Domain`

Смежные шаблоны, где определения полей лежат прямо в схеме и годятся в промпт: `R1906771` — `key limitations` = *«Weaknesses, challenges, or constraints of the method»*; `R1544480` (64 заполненных инстанса) — `architecture assumption`, `Resource Requirements`, `Disadvantage`, `Key findings`. Живая статистика ORKG на 2026-07-27: 66 026 статей, 96 637 contributions, 1 465 шаблонов.

**SciClaim** `types.json` ([siftech/SciClaim](https://github.com/siftech/SciClaim), лицензии в репозитории нет): тип **`qualifier`** = *«A span of text articulating under which conditions, locations, times, or populations a claim holds»*.

**MeasEval / SemEval-2021 Task 8** ([harperco/MeasEval](https://github.com/harperco/MeasEval), лицензии нет): `Quantity` / `MeasuredEntity` / `MeasuredProperty` / `Qualifier`, модификаторы `IsApproximate, IsCount, IsRange, IsMean, HasTolerance…`, отношения `HasQuantity, HasProperty, Qualifies`.

**IG-Bench / «Ideas Have Genomes»** (arXiv:2607.08758, подана 9 июля 2026, [VisionXLab/IdeasHaveGenomes](https://github.com/VisionXLab/IdeasHaveGenomes), лицензия «TBD»): карточка `idea_genome` из четырёх полей — `problem_genome`, `mechanism_genome`, `observation_genome`, `limitation_genome`. Ближайшая к нашей схема из найденных. Файл карточек, на который ссылается их `arena_config.py:11`, в репозитории отсутствует (404).

**Nova** ([hflyzju/Nova](https://github.com/hflyzju/Nova), `prompts/idea_examples_*.json`): 22 заполненные карточки приёмов с полями `Type, Problem, Existing Methods, Motivation, Proposed Method, Experiment Plan` — готовые few-shot примеры.

### 8.2 Датасеты для проверки извлечения

| Датасет | Лицензия | Что внутри |
|---|---|---|
| SciREX | Apache-2.0 | n-арные отношения `Method/Metric/Task/Material/Score`, полные тексты |
| MeasEval | не указана | число + единица + условие, 75 сабмишенов |
| SciClaim | не указана | 901 предложение, 12 738 меток |
| **LimitGen** (ACL 2025, `yale-nlp/LimitGen`) | — | таксономия ограничений: 4 аспекта, 10 подтипов; Cohen's κ = 0.833; 6 050 лимитаций / 1 000 статей |
| **Drug-ACE** (arXiv:2606.14031) | **CC BY 4.0** | единственная найденная работа, где «условие применимости» — целевая сущность; метрика Hard/Soft F1 |
| SciFact | `NOASSERTION` в API, CC BY-NC 2.0 в поиске — проверить | claim + evidence |
| CS-KG 2.0 | CC BY 4.0 | 67.5 млн утверждений, типы Task/Method/Material/Metric, 39 предикатов |
| pwc-archive/* | CC BY-SA 4.0 | **paperswithcode.com мёртв**, снапшот заморожен 2025-07-28, дамп `methods` засорён спамом |

### 8.3 Чего в литературе нет

Поиск по arXiv API по `"condition-effect extraction"`, `"scope of applicability"` в заголовке, `"boundary conditions" + extraction + LLM` — ноль результатов. Нет обезличенной карточки научной идеи (у IG-Bench `observation_genome` дословно тащит «GSM8K», «>100B parameters»); нет извлечения условий, которых автор не написал; нет схемы, где условие и ограничение — первоклассные поля рядом с механизмом и эффектом (у CS-KG 39 предикатов и ни одного такого; у ORKG есть — и 0 инстансов в графе); деконтекстуализация (Choi et al., TACL 2021) применялась к Википедии, не к научным тезисам.

---

## 9. Оценка

Разбор — [`09-raw/a8-eval.md`](./09-raw/a8-eval.md).

| Инструмент | Лицензия | Локально | Вердикт |
|---|---|---|---|
| **pytrec_eval** | MIT | да | **БЕРУ** — проверенные nDCG/MRR/Recall вместо ручных формул |
| **MiniCheck** (roberta-large / flan-t5-large) | репозиторий Apache-2.0, карточка модели MIT (расхождение) | CPU, 5 строк | **БЕРУ** — дешёвая проверка «подтверждается ли утверждение источником», замена T5-11B из ALCE |
| ranx, ir_measures, trec_eval | MIT / не проверено | да | РЕФЕРЕНС |
| Ragas, DeepEval, TruLens, RAGChecker, continuous-eval | Apache-2.0 / MIT | да, через OpenAI-совместимый endpoint | РЕФЕРЕНС — промпты метрик; ставить ради 1–2 метрик не стоит |
| ARES | — | требует своей разметки + ~100 ГБ | МИМО |
| Arize Phoenix | **Elastic License 2.0** | да | МИМО по лицензии |
| promptfoo | MIT (с марта 2026 в составе OpenAI) | да | РЕФЕРЕНС |
| AlignScore, SummaC, FactCC, QAFactEval | лицензии не проверены | CPU | РЕФЕРЕНС |
| G-Eval / prometheus-eval | — | — | РЕФЕРЕНС — методика рубрики LLM-судьи |

Методическое, что стоит знать при планировании замеров: судья не должен быть той же моделью, что генератор (у HiMem судья и бэкенд — одна GPT-4o-mini, [04]); для парного бинарного исхода на одном наборе запросов корректен McNemar, не t-тест; при n = 30 запросов формально уловим только эффект величиной d ≈ 0.51.

---

## 10. Трейсинг и сервер

Разбор — [`09-raw/a10-tracing-serving.md`](./09-raw/a10-tracing-serving.md).

| Инструмент | Лицензия | Цена внедрения | Вердикт |
|---|---|---|---|
| Langfuse | MIT core / платный EE | docker-compose из 5 контейнеров | МИМО |
| Arize Phoenix | Elastic License 2.0 | одна команда docker | МИМО по лицензии; кандидат в демо-дашборд |
| OpenLLMetry / OpenInference | Apache-2.0 | автоинструментация чужих SDK — нечего инструментировать при самодельном клиенте | МИМО |
| MLflow tracing | Apache-2.0 | автотрейсинг только для известных SDK | МИМО |
| Helicone | OSS, куплен, maintenance mode | proxy | МИМО |
| W&B Weave | проприетарный | аккаунт | МИМО |
| **JSONL + декоратор** | — | ~20 строк | **БЕРУ** |
| **`http.server.ThreadingHTTPServer`** | stdlib | 0 зависимостей | **БЕРУ** для `/retrieve` |
| FastAPI + uvicorn | MIT | стоит уже | РЕФЕРЕНС — если контракт дорастёт до агентного интерфейса |

OpenTelemetry GenAI semantic conventions (`gen_ai.usage.input_tokens` и т. д.) — все атрибуты на статусе **Development**, спецификация вынесена в отдельный репозиторий `semantic-conventions-genai`; подключаться рано. Счёт токенов: `usage.*` у llama.cpp надёжен при `stream=False`, при `stream=True` нужен `stream_options.include_usage=true`; офлайн — эндпоинт `/tokenize`, не tiktoken (у Qwen другой словарь). `uuid.uuid7()` появился в Python 3.14, на 3.12 — `uuid4().hex`.

---

## 11. Корпус источников

[`09-raw/a11-sources.yaml`](./09-raw/a11-sources.yaml) — 84 записи, каждая подтверждена открытием `arxiv.org/abs/<id>`; записка о слабых местах — `09-raw/a11-sources-notes.md`.

| Группа | План (`08:106-116`) | Собрано |
|---|---|---|
| Эволюционный поиск программ и LLM-мутация | ~20 | 27 |
| Агентные пайплайны и их оптимизация | ~20 | 25 |
| Дешёвые прокси и каскады оценки | ~10 | 20 |
| Память агентов и извлечение знания | ~10 | 13 (без A-MEM/HiMem/Mem0/Zep/HippoRAG/GraphRAG — они уже в [`04-papers.md`](./04-papers.md)) |

Из 84: 78 `kind=paper`, 6 `kind=doc`, 5 обзоров, 11 работ 2025–2026, **46 без HTML-версии** (в основном 2015–2023) — то есть PDF-путь §1 обслуживает больше половины корпуса. Слабые места: у 3 из 6 записей `kind=doc` вместо измеренного эффекта только параметры и флаги; у части статей (multi-agent debate, MetaGPT, AutoGen, ADAS) чисел нет в абстракте — понадобится тело статьи; инженерные блоги 2026 года не проверялись, лимит веб-поиска был исчерпан.

---

## 12. Приложение

`09-raw/probe-results.md` и скрипты `probe_llm.py`, `probe2.py`, `probe3.py`, `probe_thresholds.py` — проверка, какие из механизмов §2 и §3 фактически отвечают на серверах школы и на этой машине. Сделано попутно, к каталогу инструментов не относится; когда дойдёт до настройки, оттуда берутся готовые запросы.

