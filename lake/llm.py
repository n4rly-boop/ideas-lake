"""Schema-forced calls to the school llama.cpp servers (spec 10 §3.1).

Nothing here falls back to a default: every proven failure mode of these servers
answers HTTP 200 with plausible content — a JSON cut mid-word on `max_tokens`,
prose when the flat `response_format` is used, an empty `content` when thinking
is on, a string closed legally at its `maxLength` (probe-results.md:29-47).
So each of them raises `LLMError`, and only the network is retried.
"""
import http.client
import json
import os
import time
import urllib.error
import urllib.request

from . import trace
from .models import CANARY_SCHEMA, PROMPTS

# (base_url, name of the env var holding the key). The key is read per call, not
# at import: the module must import in an environment with no keys at all.
#
# Адрес переопределяется окружением: пул 9B школы делится со всеми, и когда в нём
# висит очередь (`requests_deferred` под две сотни, генерации по 10 минут), любой
# шаг падает по таймауту ещё на канарейке. Тогда массовые шаги временно уводятся
# на свободный сервер — правкой `.env.local` и перезапуском, без пересборки.
#
# Ключ при этом НЕ переопределяется: `LAKE_KEY_9B` — это «ключ к тому серверу,
# куда смотрит 9B». Увёл адрес на 35B — положи в ту же переменную ключ 35B.
QWEN_9B = (os.environ.get("LAKE_URL_9B") or "http://82.202.157.243:8080",
           "LAKE_KEY_9B")                                   # parse/generalize/rederive/rewrite
QWEN_35B = (os.environ.get("LAKE_URL_35B") or "http://82.202.156.206:8080",
            "LAKE_KEY_35B")                                 # link arbiter, eval judge

ATTEMPTS = 2                # §8: 2 attempts, 2 s pause, network/timeout/5xx only
RETRY_PAUSE_S = 2.0

# urllib raises TimeoutError on a read timeout and URLError on connect/DNS;
# http.client.HTTPException covers a connection dropped mid-body.
_RETRYABLE = (urllib.error.URLError, TimeoutError, http.client.HTTPException)

# Failed calls are logged too (§3.3), and `log_llm` indexes usage; a call that
# brought no answer brought no counted tokens, and its row carries finish_reason
# "error:*", so these zeros are never mistaken for a cheap successful call.
_NO_USAGE = {"prompt_tokens": 0, "completion_tokens": 0}


class LLMError(RuntimeError):
    """Any deviation from one complete, schema-conforming answer."""


def complete(prompt: str, *, system: str, schema: dict, op: str, max_tokens: int,
             timeout: float, model=QWEN_9B, temperature: float = 0.0) -> dict:
    """One schema-forced call. Returns the parsed object. Raises on any deviation."""
    base_url, key_var = model
    key = os.environ.get(key_var)
    if not key:
        raise LLMError(f"{op}: env var {key_var} is unset, no key for {base_url}")

    payload = {
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        # p.1 — nested form is the only working one; the flat form from the
        # llama.cpp README answers HTTP 200 with prose (probe-results.md:11).
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": op, "schema": schema}},
        # p.2 — unquenched thinking gives content="" (probe-results.md:34).
        "chat_template_kwargs": {"enable_thinking": False},
    }

    def log(started: float, finish_reason: str, usage: dict, build_info: str) -> None:
        trace.log_llm(op, base_url, usage, (time.monotonic() - started) * 1000.0,
                      finish_reason, max_tokens, build_info)

    last_error: Exception | None = None
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(RETRY_PAUSE_S)
        started = time.monotonic()
        try:
            data = _post(base_url, key, payload, timeout)
        except urllib.error.HTTPError as exc:        # subclass of URLError, must come first
            body = exc.read()[:400].decode("utf-8", "replace")
            log(started, f"error:http_{exc.code}", _NO_USAGE, "")
            detail = f"{op}: HTTP {exc.code} from {base_url}: {body}"
            if exc.code >= 500:                      # p.8 — server-side, worth a retry
                last_error = LLMError(detail)
                continue
            raise LLMError(detail) from exc          # 4xx is our bug, retrying repeats it
        except _RETRYABLE as exc:                    # p.8 — network and timeout
            log(started, f"error:{type(exc).__name__}", _NO_USAGE, "")
            last_error = exc
            continue
        except json.JSONDecodeError as exc:
            log(started, "error:body_not_json", _NO_USAGE, "")
            raise LLMError(f"{op}: {base_url} answered with a non-JSON HTTP body") from exc

        choices = data.get("choices") or []
        finish_reason = choices[0].get("finish_reason", "") if choices else ""
        usage = data.get("usage")
        if not isinstance(usage, dict) or {"prompt_tokens", "completion_tokens"} - set(usage):
            # usage is reliable with stream=False (09:293); zeros would understate Δcost.
            log(started, "error:no_usage", _NO_USAGE, "")
            raise LLMError(f"{op}: {base_url} answered without usage, cost accounting broken")
        # llama.cpp reports build_info on /props, not per call; read it if it ever appears.
        log(started, finish_reason, usage, str(data.get("build_info") or ""))

        if not choices:
            raise LLMError(f"{op}: {base_url} answered without choices: {str(data)[:200]}")
        # p.5 — before json.loads: on max_tokens the JSON breaks mid-word (probe-results.md:33).
        if finish_reason != "stop":
            raise LLMError(f"{op}: finish_reason={finish_reason!r}, max_tokens={max_tokens}")

        content = choices[0].get("message", {}).get("content") or ""
        try:
            obj = json.loads(content)
        except json.JSONDecodeError as exc:
            # p.6 — no retry here: this class is closed by the grammar, not by repeats.
            raise LLMError(f"{op}: content is not JSON: {content[:200]!r}") from exc
        if not isinstance(obj, dict):
            raise LLMError(f"{op}: expected a JSON object, got {type(obj).__name__}")
        _reject_truncated(obj, schema, "$")
        return obj

    raise LLMError(f"{op}: {ATTEMPTS} attempts against {base_url} failed: "
                   f"{type(last_error).__name__}: {last_error}") from last_error


def assert_grammar_works(model) -> None:
    """Canary. Raises if the server silently ignores the schema.

    One call catches all three causes of a silent ignore at once: the flat
    `response_format`, unquenched thinking and a schema dropped on the floor
    (probe-results.md:38-43). Run at the start of every run, per model used.
    """
    out = complete("Say hello in one sentence.",
                   system="Answer with JSON matching the schema.",
                   schema=CANARY_SCHEMA, op="canary", max_tokens=64, timeout=30,
                   model=model)
    if out.get("canary") != "llamacpp":
        raise LLMError(f"canary failed on {model[0]}: schema ignored, got {out!r}")


def load_prompt(step: str, name: str = "system") -> str:
    """`prompts/{step}/{name}.txt` — prompts are files, not strings in code.

    `name` is for a step that has to say something different about a different kind of
    input and must not do it with an `if` inside one text: `generalize/run.txt` is the
    same step over an evolution log, where the concrete things that must not leak are
    program ids and function names rather than dataset names (`13` §2.2.1).
    """
    path = PROMPTS / step / f"{name}.txt"
    text = path.read_text(encoding="utf-8")     # a missing file raises, no silent default
    if not text.strip():
        raise LLMError(f"empty prompt file: {path}")
    return text


def _post(base_url: str, key: str, payload: dict, timeout: float) -> dict:
    """Same request shape as the verified probe (09-raw/probe_llm.py:18-25)."""
    req = urllib.request.Request(
        base_url + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    # p.4 — timeout is mandatory: a hung socket raises nothing and pending_link
    # never sees it (§0.1.15).
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _reject_truncated(value, schema: dict, path: str) -> None:
    """p.7 — a string exactly as long as its declared maxLength is a cut-off word.

    The grammar cuts mid-word and closes the string legally: finish_reason="stop",
    valid JSON, json.loads passes (probe-results.md:47). Hence equality with the
    ceiling counts as damage. False positives are acceptable — ceilings carry
    slack precisely so they are never reached (§3.1).
    """
    cap = schema.get("maxLength")
    if isinstance(value, str) and cap is not None and len(value) == cap:
        raise LLMError(f"truncated at maxLength={cap}: {path} ends {value[-60:]!r}")
    if isinstance(value, dict):
        for name, sub in (schema.get("properties") or {}).items():
            if name in value:
                _reject_truncated(value[name], sub, f"{path}.{name}")
    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                _reject_truncated(item, items, f"{path}[{i}]")


if __name__ == "__main__":
    from .models import GENERALIZE_SCHEMA, PARSE_SCHEMA

    def cut(obj, schema) -> bool:
        try:
            _reject_truncated(obj, schema, "$")
        except LLMError:
            return True
        return False

    item = {"text": "x" * 699, "context": "c", "effect": "e", "locator": "l",
            "draft_text": "d", "draft_applicability": "a", "draft_limitations": "L"}
    assert not cut({"theses": [item]}, PARSE_SCHEMA)
    assert cut({"theses": [item, {**item, "text": "x" * 700}]}, PARSE_SCHEMA)    # deep in an array
    assert cut({"theses": [{**item, "locator": "y" * 120}]}, PARSE_SCHEMA)       # short field counts too
    assert not cut({"canary": "llamacpp"}, CANARY_SCHEMA)                        # no maxLength anywhere

    gen = {"text": "t", "applicability_conditions": "a", "limitations": "l",
           "failure_modes": ["f" * 299]}
    assert not cut(gen, GENERALIZE_SCHEMA)
    assert cut({**gen, "failure_modes": ["ok", "f" * 300]}, GENERALIZE_SCHEMA)   # string inside an array

    try:
        complete("hi", system="s", schema=CANARY_SCHEMA, op="canary", max_tokens=16,
                 timeout=5, model=("http://127.0.0.1:1", "LAKE_KEY_ABSENT_FOR_SELFCHECK"))
    except LLMError as exc:
        assert "LAKE_KEY_ABSENT_FOR_SELFCHECK" in str(exc), exc
    else:
        raise AssertionError("a missing key must raise before any socket is opened")

    # Переезд массовых шагов на свободный сервер — это правка окружения, и она
    # должна быть видна в паре, которую получают шаги, а не только в комментарии.
    import importlib
    saved = os.environ.get("LAKE_URL_9B")
    os.environ["LAKE_URL_9B"] = "http://example.invalid:9999"
    try:
        moved = importlib.reload(importlib.import_module(__spec__.name))
        assert moved.QWEN_9B == ("http://example.invalid:9999", "LAKE_KEY_9B"), moved.QWEN_9B
        assert moved.QWEN_35B[0] == "http://82.202.156.206:8080", moved.QWEN_35B
    finally:
        if saved is None:
            del os.environ["LAKE_URL_9B"]
        else:
            os.environ["LAKE_URL_9B"] = saved
        importlib.reload(importlib.import_module(__spec__.name))

    print("ok: maxLength truncation detector, missing-key guard, LAKE_URL_9B override")
