# Живые замеры на серверах школы — 2026-07-27

Скрипты: `probe_llm.py`, `probe2.py`, `probe3.py` (stdlib urllib, ключи читаются из ноутбука, наружу не выводятся).
Билды: `Qwen3.5-9B-Q8_0.gguf`, `Qwen3.6-35B-A3B-Q8_0.gguf`, `Qwen3.6-27B-Heretic-Q6_K.gguf` (`/v1/models`).

## 1. Structured output — что работает

| Механизм | 9B | 35B | 27B | Комментарий |
|---|---|---|---|---|
| `response_format.json_schema.schema` (вложенно) | ✅ валидный JSON | ✅ | ✅ | рабочая форма |
| `response_format.schema` (плоско, как в README llama.cpp) | ❌ свободный текст, HTTP 200 | ❌ | ❌ | **тихая деградация**, схема игнорируется |
| `response_format.type=json_object` + `schema` наверху | ✅ | ✅ | ✅ | тоже рабочая форма |
| `response_format.type=json_object` без схемы | ❌ проза, не JSON | ❌ | ❌ | не принуждает |
| `tools` + `tool_choice=required\|auto` | ✅ `finish_reason=tool_calls` | ✅ | ✅ | `arguments` — **строка** с валидным JSON |
| `grammar` (GBNF) в `/v1/chat/completions` | ✅ | ✅ | ✅ | работает и на chat-эндпоинте |
| `/v1/embeddings` | ❌ **HTTP 501** | ❌ 501 | ❌ 501 | `This server does not support embeddings. Start it with --embeddings` |
| `/tokenize` | ✅ | ✅ | ✅ | офлайн-счёт токенов есть |

## 2. Цена

| Замер | Число |
|---|---|
| Оверхед `tools` в prompt_tokens (одна функция, 1 поле) | **+312 токенов на вызов**, одинаково у всех трёх моделей (13 → 325) |
| Оверхед `json_schema` в prompt_tokens | **0** (грамматика на сервере, в промпт не попадает) |
| Латентность одиночного вызова извлечения (~400 out-токенов, 9B) | 7.0 с |
| 8 параллельных на 9B | wall 15.5 с, 207 tok/s суммарно, 10.8 с на запрос, 0 ошибок |
| 27B с thinking | до 59 с на вызов |

## 3. Режимы отказа (проверены, не гипотезы)

| Отказ | Что видно | Как ловить |
|---|---|---|
| Упор в `max_tokens` при активной схеме/tools | `finish_reason="length"`, JSON **обрывается на полуслове** (и в `content`, и в `arguments`) | проверять `finish_reason` **до** `json.loads`; это главный fail-open |
| Thinking не погашен | `reasoning_content` 1100+ символов, `content=""`, `finish_reason="length"` — у всех трёх моделей | всегда слать `chat_template_kwargs.enable_thinking=false` **или** `reasoning_effort="none"` (оба работают) |
| Thinking + tools на 35B/27B | tool call **не выдан вообще** за 1500 токенов (5860/5143 симв. рассуждений) | извлечение гоняем только с погашенным thinking |
| Плоская форма `response_format` | HTTP 200 + проза | канарейка на старте прогона (схема с `const`) |

Канарейка (ловит все три причины молчаливого игнора схемы разом):
```python
CANARY = {"type":"object","properties":{"canary":{"const":"llamacpp"}},
          "required":["canary"],"additionalProperties":False}
# промпт "Say hello in one sentence." → грамматика есть: {"canary":"llamacpp"}; нет: "Hello!"
```

## 4. Грамматика реально принуждает

Схема с `maxItems: 2`, `maxLength: 40` на запрос «дай 8 приёмов»: все три модели вернули ровно 2 элемента, строки обрезаны ровно на 40 символах — **посреди слова** (`'Population Diversity Maintenance (e.g., '`). Вывод: `maxLength` — предохранитель от разгона, а не форматирование; ставить с запасом.

## 5. Что из этого следует для плана 08

1. `08:153` («structured output через function calling») → перейти на `response_format.json_schema`: та же жёсткость (GBNF на сервере), но **−312 токенов на вызов** и нет зависимости от парсера tool-call'ов Qwen (у которого открытые баги).
2. `08:88` (`trace.py`) → `finish_reason` и `build_info` сервера писать в трейс: половина багов привязана к номеру сборки.
3. **`08:228` (эмбеддинги) — дыра**: `snowflake-arctic-embed-s` считать негде, сервера отдают 501. Либо локально на CPU (33M параметров — секунды на 2000 тезисов), либо просить админов поднять инстанс с `--embeddings`. В плане это место не названо.
4. `08:393` (стоимость записи) → есть первые числа: 8 параллельных потоков на 9B дают 207 tok/s; корпус в ~500 вызовов извлечения проходит примерно за 10 минут, а не за часы.

