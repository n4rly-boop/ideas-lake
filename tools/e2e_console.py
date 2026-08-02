"""tools/e2e_console.py — headless-Chrome + CDP driver for lake/api/console.html.

Boots its OWN `lake.api.app` instance (so its access log is ours to read) plus a
headless Chrome, wires the two together over the Chrome DevTools Protocol (the
`websockets` package, already installed — no new dependency), and runs a small
registry of browser tests against the result.

Run everything:

    python3 tools/e2e_console.py

Run one test, keep the server/Chrome up afterwards to poke by hand:

    python3 tools/e2e_console.py --only dial_answers --keep

Add a test — decorate a function taking `(browser, base_url)`:

    @test("my_thing")
    def test_my_thing(browser, base_url):
        browser.goto(f"{base_url}/ui#ideas")
        browser.wait_for("document.querySelector('.idea-card') !== null")
        assert browser.evaluate("document.title") != "", "empty title"

`browser` is a `Browser` (see its docstring for the full helper list: goto,
evaluate, wait_for, set_device, tap, click, press, fill, screenshot, server_log,
count_requests). Raise or assert on failure — the runner catches it, prints
`FAIL <name>: <reason>` with a traceback, and keeps going.

Fetch data straight from the API with `urllib.request` against `base_url` when a
test needs to compare what the page shows against what the server actually
returned (`--no-auth`, so no header dance needed): the page is never the source
of truth for a number, the API is.
"""
import argparse
import asyncio
import base64
import fcntl
import itertools
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env.local"
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def read_env_file(path: Path) -> dict:
    """Simple `KEY=VALUE` parser: blank lines and `#` comments are skipped, nothing
    else is interpreted (no quoting, no expansion) — `.env.local` here is that plain."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def writer_lock_free() -> bool:
    """Non-blocking probe of the exact flock `lake/writer_lock.py` takes on
    `lake/data/writer.lock` — acquired and released immediately, never held past this
    call. True means a full (non-`--mock`) server can safely start; false means some
    other process (typically the standing server on :8077) already holds it and a
    second full instance would die at startup with `SecondWriter` before /healthz
    ever answers (confirmed by hand)."""
    path = REPO_ROOT / "lake" / "data" / "writer.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return True
    except OSError:
        return False


def free_port() -> int:
    # ponytail: bind-then-release has a TOCTOU gap between this and the child
    # process actually binding it; fine for a local, sequential e2e run, not for
    # anything parallel — reserve properly (SO_REUSEPORT dance) if that's ever needed.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _poll_http_ok(url: str, proc: subprocess.Popen, timeout: float, what: str, log_path: Path):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log = log_path.read_text() if log_path.exists() else "(no log file)"
            raise RuntimeError(f"{what} exited early (code {proc.returncode}); log:\n{log}")
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.3)
    log = log_path.read_text() if log_path.exists() else "(no log file)"
    raise TimeoutError(f"{what} did not answer {url} within {timeout}s; log:\n{log}")


class LakeServer:
    """One `lake.api.app` process, its own port, its own log file."""

    def __init__(self, port: int, log_path: Path):
        self.port = port
        self.log_path = log_path
        self.base_url = f"http://127.0.0.1:{port}"
        self.proc = None
        self._log_fh = None

    def start(self, mock: bool):
        env = os.environ.copy()
        env.update(read_env_file(ENV_FILE))
        # The local stand's Neo4j — fixed, not read from .env.local (that file points
        # compose's NEO4J_URI at the `neo4j` service name, unreachable from the host).
        env.update({
            "NEO4J_URI": "bolt://127.0.0.1:7687",
            "NEO4J_USERNAME": "neo4j",
            "NEO4J_PASSWORD": "none",
            "NEO4J_DATABASE": "neo4j",
        })
        cmd = [sys.executable, "-m", "lake.api.app",
               "--port", str(self.port), "--host", "127.0.0.1", "--no-auth"]
        if mock:
            # The already-running stand on :8077 holds the phase-2 writer lock
            # (`lake/data/writer.lock`, one writer per lake dir, `flock`-enforced). A
            # second full instance dies at startup with `SecondWriter` before /healthz
            # ever answers — confirmed by hand before writing this driver. --mock only
            # freezes /retrieve, /research, /fetch and /run; /dial, /ui, /healthz and
            # every graph read (the ones most of these tests use) stay real, reading
            # the same index and the same Neo4j as the stand. `main()` decides `mock`
            # by probing the lock (`writer_lock_free()`) and says so on stdout when it
            # has to fall back — silently degrading which paths got tested is exactly
            # the kind of fail-open this project bans.
            cmd.append("--mock")
        self._log_fh = open(self.log_path, "w")
        self.proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env,
                                      stdout=self._log_fh, stderr=subprocess.STDOUT)

    def wait_ready(self, timeout=60):
        _poll_http_ok(f"{self.base_url}/healthz", self.proc, timeout, "lake.api.app", self.log_path)

    def stop(self):
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if self._log_fh:
            self._log_fh.close()


class Chrome:
    """One headless Chrome process with a remote-debugging port of its own."""

    def __init__(self, port: int, user_data_dir: Path):
        self.port = port
        self.user_data_dir = user_data_dir
        self.devtools_url = f"http://127.0.0.1:{port}"
        self.proc = None

    def start(self):
        cmd = [CHROME_PATH, "--headless=new", "--disable-gpu",
               f"--remote-debugging-port={self.port}",
               f"--user-data-dir={self.user_data_dir}",
               "--no-first-run", "--no-default-browser-check"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait_ready(self, timeout=20):
        _poll_http_ok(f"{self.devtools_url}/json/version", self.proc, timeout, "chrome",
                      Path(os.devnull))

    def stop(self):
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


class _CDPLoop:
    """One asyncio event loop, owned by a background thread, so `Browser`'s public
    methods can stay plain synchronous calls and a test can read top to bottom."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro, timeout):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


class Browser:
    """Synchronous facade over one Chrome tab, driven through CDP over websockets.

    goto(url)                        Page.navigate, waits for document.readyState complete.
    evaluate(js) -> value             Runtime.evaluate (awaitPromise, returnByValue).
                                      Raises on `exceptionDetails` — a broken page must
                                      not read back as `None`.
    wait_for(js_expr, timeout=10)     Polls evaluate() until truthy, else TimeoutError.
    set_device(w, h, mobile=, touch=) Emulation.setDeviceMetricsOverride + touch flags.
    clear_device()                    Undoes set_device — back to the real viewport. The
                                      runner calls this before EVERY test (not just the
                                      ones that call set_device), so registration order
                                      can never make a later test inherit an earlier
                                      test's device override.
    add_init_script(js) -> id         Runs `js` before the target's own scripts, on every
                                      navigation from here on — the only way to patch
                                      something console.html's boot code reads (e.g.
                                      `window.fetch` before `probe()` calls it).
    remove_init_script(id)            Undoes add_init_script — call in a `finally`.
    tap(x, y)                         Input.dispatchTouchEvent touchStart/touchEnd.
    click(selector)                   Real mouse: center of the element, dispatchMouseEvent.
    press(selector, key)              Real keyboard: dispatchKeyEvent (e.g. "Enter").
    fill(selector, text)              Sets .value, dispatches input+change.
    screenshot(path)                  Page.captureScreenshot, full page height.
    server_log() -> str               Current content of the server's log file.
    count_requests(pattern) -> int    Access-log lines containing `pattern`.
    """

    def __init__(self, devtools_port: int, log_path: Path = None):
        self._runner = _CDPLoop()
        self._ws = None
        self._ids = itertools.count(1)
        self._pending = {}   # id -> Future, touched only on the loop thread
        self._nav_counter = itertools.count(1)
        self.log_path = log_path
        self._runner.run(self._connect(devtools_port), timeout=15)

    async def _connect(self, devtools_port):
        with urllib.request.urlopen(f"http://127.0.0.1:{devtools_port}/json/list", timeout=5) as r:
            tabs = json.loads(r.read())
        page = next((t for t in tabs if t.get("type") == "page"), None)
        if page is None:
            with urllib.request.urlopen(f"http://127.0.0.1:{devtools_port}/json/new", timeout=5) as r:
                page = json.loads(r.read())
        self._ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=None)
        asyncio.get_event_loop().create_task(self._read_loop())
        await self._send("Page.enable")
        await self._send("Runtime.enable")

    async def _read_loop(self):
        # Only command responses ({"id": ...}) are handled — no test here needs a raw
        # CDP event, and `goto()` below polls `document.readyState` instead of
        # `Page.loadEventFired` (which never fires again for a same-document,
        # hash-only navigation — found running this file against `#dial`).
        async for raw in self._ws:
            msg = json.loads(raw)
            if "id" not in msg:
                continue
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(f"CDP error: {msg['error']}"))
                else:
                    fut.set_result(msg.get("result", {}))

    async def _send(self, method, params=None, timeout=30):
        mid = next(self._ids)
        fut = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)

    def _call(self, method, params=None, timeout=30):
        return self._runner.run(self._send(method, params, timeout), timeout + 5)

    def goto(self, url, timeout=30):
        # A same-path, hash-only `goto()` (e.g. "#ideas" -> "#theses") is a same-document
        # navigation to Chrome — no reload, so every page global (AUTH, API_KEY,
        # GRAPH_CACHE, generation counters, a test's own window.fetch patch) survives
        # into whatever runs next. A unique query parameter ahead of the hash makes the
        # URL different every time, which forces a REAL document load — proven by
        # `navigation_reloads_the_document` below. Every test's first browser action is a
        # goto(), so this alone is what gives each test a clean page.
        path, _, frag = url.partition("#")
        sep = "&" if "?" in path else "?"
        nav_url = f"{path}{sep}_t={next(self._nav_counter)}" + (f"#{frag}" if frag else "")
        self._call("Page.navigate", {"url": nav_url}, timeout=timeout)
        # A hash-only navigation (`#dial` on a page already loaded) never fires
        # another `Page.loadEventFired` — it is the same document. Polling
        # `readyState` covers both that case and a real cross-document load, and
        # tolerates the execution context briefly going away mid-navigation instead
        # of tripping over it.
        deadline = time.monotonic() + timeout
        last_err = None
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    return
            except Exception as exc:
                last_err = exc
            time.sleep(0.05)
        raise TimeoutError(f"page never reached readyState=complete within {timeout}s "
                            f"navigating to {url} (last error: {last_err})")

    def evaluate(self, js, timeout=30):
        result = self._call("Runtime.evaluate", {
            "expression": js, "returnByValue": True, "awaitPromise": True,
        }, timeout)
        if "exceptionDetails" in result:
            d = result["exceptionDetails"]
            desc = (d.get("exception") or {}).get("description") or d.get("text") or str(d)
            # Raise, don't return None: a broken page reading back as an empty/falsy
            # value is exactly the fail-open this project's own rules forbid on the
            # server side, and a test harness that does it too would be a lie twice.
            raise RuntimeError(f"page threw during evaluate: {desc}\n  expression: {js}")
        return result.get("result", {}).get("value")

    def wait_for(self, js_expr, timeout=10):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.evaluate(js_expr)
            if last:
                return last
            time.sleep(0.2)
        raise TimeoutError(f"never became truthy within {timeout}s (last={last!r}): {js_expr}")

    def set_device(self, width, height, mobile=False, touch=False):
        self._call("Emulation.setDeviceMetricsOverride",
                    {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": mobile})
        # maxTouchPoints must be 1-16 when given at all — CDP rejects 0 outright, so
        # it is only passed when touch is actually being turned on.
        self._call("Emulation.setTouchEmulationEnabled",
                    {"enabled": touch, **({"maxTouchPoints": 5} if touch else {})})
        self._call("Emulation.setEmitTouchEventsForMouse",
                    {"enabled": touch, "configuration": "mobile" if touch else "desktop"})

    def clear_device(self):
        self._call("Emulation.clearDeviceMetricsOverride")
        self._call("Emulation.setTouchEmulationEnabled", {"enabled": False})
        self._call("Emulation.setEmitTouchEventsForMouse",
                    {"enabled": False, "configuration": "desktop"})

    def add_init_script(self, source: str) -> str:
        """`Page.addScriptToEvaluateOnNewDocument` — runs before ANY of the target
        document's own scripts, on every navigation from here on. The only way to patch
        something (e.g. `window.fetch`) that console.html's inline <script> reads at boot,
        since a post-`goto()` `evaluate()` always runs too late for that. Returns the CDP
        script identifier; the caller MUST pass it to `remove_init_script` when done, or
        every later test's `goto()` inherits the same patch."""
        return self._call("Page.addScriptToEvaluateOnNewDocument", {"source": source})["identifier"]

    def remove_init_script(self, identifier: str):
        self._call("Page.removeScriptToEvaluateOnNewDocument", {"identifier": identifier})

    def tap(self, x, y):
        self._call("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [
            {"x": x, "y": y, "radiusX": 5, "radiusY": 5, "force": 1}]})
        time.sleep(0.05)
        self._call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    def _center_of(self, selector):
        js = ("(() => { const el = document.querySelector(%s); "
              "if (!el) throw new Error('no element for selector: ' + %s); "
              "el.scrollIntoView({block: 'center', inline: 'center'}); "
              "const r = el.getBoundingClientRect(); "
              "return {x: r.x + r.width / 2, y: r.y + r.height / 2}; })()"
              % (json.dumps(selector), json.dumps(selector)))
        return self.evaluate(js)

    def click(self, selector):
        pt = self._center_of(selector)
        self._call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": pt["x"], "y": pt["y"]})
        self._call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": pt["x"], "y": pt["y"],
                                                  "button": "left", "clickCount": 1})
        self._call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": pt["x"], "y": pt["y"],
                                                  "button": "left", "clickCount": 1})

    _KEYS = {
        "Enter": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13,
                  "nativeVirtualKeyCode": 13, "text": "\r"},
        "Escape": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27,
                   "nativeVirtualKeyCode": 27},
    }

    def press(self, selector, key):
        self.evaluate("document.querySelector(%s).focus()" % json.dumps(selector))
        spec = self._KEYS.get(key, {"key": key, "code": key})
        self._call("Input.dispatchKeyEvent", {"type": "keyDown", **spec})
        self._call("Input.dispatchKeyEvent", {"type": "keyUp", **spec})

    def fill(self, selector, text):
        js = ("(() => { const el = document.querySelector(%s); "
              "if (!el) throw new Error('no element for selector: ' + %s); "
              "el.value = %s; "
              "el.dispatchEvent(new Event('input', {bubbles: true})); "
              "el.dispatchEvent(new Event('change', {bubbles: true})); })()"
              % (json.dumps(selector), json.dumps(selector), json.dumps(text)))
        self.evaluate(js)

    def screenshot(self, path):
        metrics = self._call("Page.getLayoutMetrics")
        size = metrics.get("cssContentSize") or metrics["contentSize"]
        shot = self._call("Page.captureScreenshot", {
            "format": "png", "captureBeyondViewport": True,
            "clip": {"x": 0, "y": 0, "width": size["width"], "height": size["height"], "scale": 1},
        }, timeout=30)
        Path(path).write_bytes(base64.b64decode(shot["data"]))

    def server_log(self) -> str:
        return self.log_path.read_text() if self.log_path and self.log_path.exists() else ""

    def count_requests(self, pattern: str) -> int:
        return sum(1 for line in self.server_log().splitlines() if pattern in line)

    def close(self):
        try:
            self._runner.run(self._ws.close(), 5)
        except Exception:
            pass
        self._runner.stop()


# ============================================================================ tests

TESTS = []
PAGE_TABS = ("ideas", "theses", "sources")   # the three paged list views, in tab order


def test(name, expected_fail=False):
    """`expected_fail=True` registers a test the codebase is KNOWN to still fail (the
    guard it checks for is not written yet). Such a test prints 'XFAIL <name>' on
    failure and does not fail the run — but if it ever PASSES, that is news (someone
    shipped the guard, or the test rotted into not testing anything) and it prints
    'XPASS <name>' and DOES fail the run. This is the only way to land the check
    before the code: a plain green test on an unimplemented guard is worse than no
    test, and a plain failing test blocks every unrelated change."""
    def deco(fn):
        TESTS.append((name, fn, expected_fail))
        return fn
    return deco


# `fmt()` in console.html prints numbers through toLocaleString("ru-RU"), which groups
# thousands with a locale-specific space — U+00A0 (no-break) or U+202F (narrow no-break)
# depending on the engine's ICU data. `\s` in Python's `re` already matches both (it is
# Unicode-aware by default), but they are spelled out below anyway so the intent reads
# without having to know that.
_THOUSANDS_SEP = r"[\s  ]"


def status_text_sans_code(browser: Browser, host_selector: str = "#main .status") -> str:
    """The status line's message, with the `.code` badge (e.g. an HTTP status) stripped
    out first. Reading `.textContent` of the whole `.status` div lets a number that is
    part of the status CODE bleed into a number the text is trying to report — e.g.
    total=200 next to a 200 response renders as the code and the count running
    together with no separator (`200 200 листьев`), and a naive substring match over
    the combined text can no longer tell "the count is right" from "the count is
    missing and the code happens to look like it"."""
    js = ("(() => { const box = document.querySelector(%s); if (!box) return null; "
          "const clone = box.cloneNode(true); const code = clone.querySelector('.code'); "
          "if (code) code.remove(); return clone.textContent; })()" % json.dumps(host_selector))
    text = browser.evaluate(js)
    assert text is not None, f"no element for selector: {host_selector!r}"
    return text


def extract_count_before(text: str, anchor: str) -> int:
    """Pull the integer immediately before a literal word (e.g. `total` printed as
    `12 345 листьев`) out of already-code-stripped status text, grouping digits split
    by any thousands separator. Anchored on the following word rather than "the first
    number in the string" so a decimal like a cosine score earlier in the sentence
    cannot be mistaken for the count."""
    m = re.search(r"([\d" + _THOUSANDS_SEP[1:-1] + r"]+)\s*" + re.escape(anchor), text)
    assert m, f"no {anchor!r}-prefixed count found in: {text!r}"
    return int(re.sub(_THOUSANDS_SEP, "", m.group(1)))


def read_labeled_number(browser: Browser, label_text: str, root: str = "#main") -> str:
    """The value of the `<input>` inside the `label.f` whose own text is exactly
    `label_text` (e.g. the "k" field) — read from the live DOM instead of assumed,
    because the page's own default is the only thing that does not drift when the
    markup changes under a hardcoded test."""
    js = ("(() => { const labels = [...document.querySelectorAll(%s + ' label.f')]; "
          "const lab = labels.find((l) => l.textContent.trim() === %s); "
          "const input = lab && lab.querySelector('input'); return input ? input.value : null; })()"
          % (json.dumps(root), json.dumps(label_text)))
    value = browser.evaluate(js)
    assert value is not None, f"no label.f in {root!r} with exact text {label_text!r}"
    return value


def set_labeled_input(browser: Browser, label_text: str, value: str, root: str = "#main") -> None:
    """The write-side twin of `read_labeled_number`: sets the `<input>` inside the
    `label.f` whose own text is exactly `label_text`, dispatching input/change like a real
    keystroke would (matches `Browser.fill`'s own dispatch, even though the PATCH form's
    click handler only reads `.value` directly and would work without it)."""
    js = ("(() => { const labels = [...document.querySelectorAll(%s + ' label.f')]; "
          "const lab = labels.find((l) => l.textContent.trim() === %s); "
          "const input = lab && lab.querySelector('input'); "
          "if (!input) throw new Error('no label.f in ' + %s + ' with exact text ' + %s); "
          "input.value = %s; "
          "input.dispatchEvent(new Event('input', {bubbles: true})); "
          "input.dispatchEvent(new Event('change', {bubbles: true})); })()"
          % (json.dumps(root), json.dumps(label_text), json.dumps(root), json.dumps(label_text),
             json.dumps(value)))
    browser.evaluate(js)


def click_button_with_text(browser: Browser, text: str, root: str = "#main") -> None:
    """Click the first `<button>` under `root` whose trimmed text matches exactly —
    `console.html` reuses `button.act`/`button.ghost` across multiple buttons per view,
    so a class-based CSS selector alone cannot tell "Открыть" from "Записать изменения".
    `.click()` on the real element fires a real "click" event (the page wires handlers
    with `addEventListener`, see `el()`), same as a user's mouse."""
    js = ("(() => { const btns = [...document.querySelectorAll(%s + ' button')]; "
          "const b = btns.find((x) => x.textContent.trim() === %s); "
          "if (!b) throw new Error('no button with text ' + %s + ' under ' + %s); "
          "b.click(); })()"
          % (json.dumps(root), json.dumps(text), json.dumps(text), json.dumps(root)))
    browser.evaluate(js)


_PAGER_RE = re.compile(
    r"показано\s+([\d\s  ]+)–([\d\s  ]+)\s+из\s+([\d\s  ]+)")


def parse_pager(text: str):
    """`"показано 1–25 из 137 (...)"` -> (1, 25, 137), stripping thousands separators
    from each number. The dash is U+2013 (en dash) — the exact character console.html
    writes, checked against the source rather than guessed."""
    m = _PAGER_RE.search(text)
    assert m, f"pager text does not match the expected 'показано A–B из C' shape: {text!r}"
    def num(s):
        return int(re.sub(r"[\s  ]", "", s))
    return num(m.group(1)), num(m.group(2)), num(m.group(3))


def click_pager_forward(browser: Browser, before_text: str, timeout: float = 10):
    """Click the pager's second button (`pager()` in console.html always emits
    [back, forward, range] in that order) and wait for the range text to actually
    change — not just for the click to fire, since a request in flight has not
    committed anything to the DOM yet."""
    browser.click("#main .pager button:nth-child(2)")
    browser.wait_for("(() => { const p = document.querySelector('#main .pager'); "
                      "return p && p.textContent !== %s; })()" % json.dumps(before_text),
                      timeout=timeout)


def measure_overflow(browser: Browser):
    """`document.documentElement.scrollWidth` is blind to overflow that lives inside its
    own `overflow-x:auto` container — nav.tabs, header.bar (via flex-wrap), a table's
    `.scroller` all swallow their own sideways overflow without ever making the document
    itself wider than the viewport. Measured on the actual elements instead."""
    js = """
    (() => {
      const out = { docOverflow: document.documentElement.scrollWidth > window.innerWidth,
                    innerWidth: window.innerWidth,
                    docScrollWidth: document.documentElement.scrollWidth,
                    elements: [] };
      const check = (el, label) => {
        if (el && el.scrollWidth > el.clientWidth + 1) {
          out.elements.push(label + ": scrollWidth=" + el.scrollWidth + " clientWidth=" + el.clientWidth);
        }
      };
      check(document.querySelector('nav.tabs'), 'nav.tabs');
      check(document.querySelector('header.bar'), 'header.bar');
      document.querySelectorAll('#main .scroller').forEach((el, i) => check(el, '#main .scroller[' + i + ']'));
      return out;
    })()
    """
    return browser.evaluate(js)


def header_nav_overlap(browser: Browser):
    """§2.3: `nav.tabs{top:53px}` is hardcoded to a single-line header's height. On a
    wrapped header (narrow width, `header.bar{flex-wrap:wrap}`) the header renders taller
    than 53px and the sticky tab strip — pinned to that fixed number — is drawn UNDER it
    instead of below it. Compares the header's real rendered bottom edge to the tab
    strip's real top edge instead of trusting the CSS constant either side assumes."""
    js = """
    (() => {
      const header = document.querySelector('header.bar');
      const nav = document.querySelector('nav.tabs');
      if (!header || !nav) return null;
      return { headerBottom: header.getBoundingClientRect().bottom,
               navTop: nav.getBoundingClientRect().top };
    })()
    """
    return browser.evaluate(js)


def _install_healthz_override(browser: Browser, response_js: str) -> str:
    """Registers an init script (`add_init_script`) that wraps `window.fetch` so any
    request whose URL contains "/healthz" runs `response_js` instead of hitting the real
    network — everything else passes through untouched. `response_js` is raw JS that must
    `return` a Promise (a rejection for a network failure, or `Promise.resolve(new
    Response(...))` for a real status code). Must run before the page's own <script>, which
    is exactly what `add_init_script` (unlike a post-goto `evaluate`) guarantees."""
    script = (
        "(() => { const orig = window.fetch; "
        "window.fetch = function(input, init) { "
        "const url = typeof input === 'string' ? input : (input && input.url) || ''; "
        "if (url.includes('/healthz')) { %s } "
        "return orig.apply(this, arguments); }; })();"
    ) % response_js

    return browser.add_init_script(script)


def _reject_js() -> str:
    return 'return Promise.reject(new TypeError("e2e-injected: offline"));'


def _response_js(status: int, body: dict, delay_ms: int = 0) -> str:
    body_literal = json.dumps(json.dumps(body))   # JSON text, then re-quoted as a JS string
    make = ("Promise.resolve(new Response(%s, {status: %d, "
            "headers: {'Content-Type': 'application/json'}}))") % (body_literal, status)
    if delay_ms:
        return "return new Promise((resolve) => setTimeout(() => resolve(%s.then((r) => r)), %d));" \
            % (make, delay_ms)
    return "return %s;" % make


def find_small_theses_idea(base_url: str, scan_limit: int = 50, page_limit: int = 25):
    """First idea (scanning `/ideas`) whose OWN thesis count fits in a single `/theses`
    page — used to exercise the pager's last-page boundary without depending on a lucky
    total across the whole store. Returns `(idea_id, its thesis count)`.

    `page_limit=25` mirrors console.html's own `PAGE` constant (console.html:1148), kept
    as a plain number rather than parsed out of the page — same as `pager_advances` reads
    the real page size off the rendered pager instead of assuming it."""
    with urllib.request.urlopen(f"{base_url}/ideas?limit={scan_limit}&offset=0", timeout=10) as r:
        ideas = json.load(r)["items"]
    for idea in ideas:
        n = len(idea["theses"])
        if 1 <= n <= page_limit:
            return idea["id"], n
    raise AssertionError(f"no idea in the first {scan_limit} has between 1 and {page_limit} "
                          f"theses — pager_stops_at_the_end needs one to test the last-page boundary")


@test("loads")
def test_loads(browser: Browser, base_url: str):
    with urllib.request.urlopen(f"{base_url}/ui", timeout=10) as r:
        assert r.status == 200, f"GET /ui -> {r.status}"

    browser.goto(f"{base_url}/ui")
    browser.wait_for("document.querySelectorAll('nav.tabs button').length > 0", timeout=10)
    count = browser.evaluate("document.querySelectorAll('nav.tabs button').length")
    # Measured directly against this commit (view() is called 9 times in
    # console.html: retrieve, dial, search, ideas, theses, sources, ingest, graph,
    # raw) — 9 tab buttons render, not 8. Asserting the real, measured number rather
    # than the brief's guess, per the same rule the API itself is held to: a count
    # is either right or it says nothing.
    assert count == 9, f"expected 9 tab buttons (nav.tabs button), found {count}"


@test("navigation_reloads_the_document")
def test_navigation_reloads_the_document(browser: Browser, base_url: str):
    """A same-path, hash-only `goto()` used to be a same-document navigation — DOM and
    every page global (AUTH, API_KEY, GRAPH_CACHE, a test's own window.fetch patch) stayed
    alive across it. Proven directly with a stamp: set it, navigate to a different hash on
    the same path, expect it gone. If this ever fails, every test below it is suspect —
    each of them relies on starting from a clean document."""
    browser.goto(f"{base_url}/ui#ideas")
    browser.evaluate("window.__e2eStamp = 'leftover-from-previous-test'")
    assert browser.evaluate("window.__e2eStamp") == "leftover-from-previous-test"

    browser.goto(f"{base_url}/ui#theses")
    survived = browser.evaluate("window.__e2eStamp")
    assert survived is None, (
        f"window.__e2eStamp survived a goto() to a new hash on the same path: {survived!r} "
        f"— navigation is not reloading the document, later tests will inherit earlier "
        f"tests' state")


@test("dial_answers")
def test_dial_answers(browser: Browser, base_url: str):
    hypothesis = "schedule mutations by how often a child beats its parent"

    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    k = read_labeled_number(browser, "k")   # the page's own default, not a copy of it
    browser.fill("#main textarea", hypothesis)
    browser.click("#main button.act")
    browser.wait_for("document.querySelector('.graphwrap svg') !== null", timeout=20)

    rows = browser.evaluate("document.querySelectorAll('#main table.tbl tbody tr').length")
    status_class = browser.evaluate("document.querySelector('#main .status').className")
    status_text = status_text_sans_code(browser)

    q = urllib.parse.quote(hypothesis)
    with urllib.request.urlopen(f"{base_url}/dial?q={q}&k={k}", timeout=15) as r:
        data = json.loads(r.read())

    assert rows == len(data["hits"]), (
        f"hit table has {rows} rows, GET /dial returned {len(data['hits'])} hits")
    assert rows == int(k), f"hit table has {rows} rows, page's own default k is {k}"

    # A page that always claims "числам верить нельзя" (lost=true, status class "warn")
    # would still get `total` right below and slip past a check that only reads
    # digits — the CLASS is the only place "did the page trust its own numbers" shows
    # up, and a healthy stand must land on "ok", not "warn"/"err".
    assert status_class.split() == ["status", "ok"], (
        f"expected '.status' class 'status ok' on a healthy stand, got {status_class!r} "
        f"(text: {status_text!r})")

    # `total` is read out of the message with `.code` already stripped, anchored on the
    # literal word "листьев" that always follows it in both console.html branches — a
    # bare substring check over the whole status text (code included) degenerates into
    # a tautology whenever total happens to equal the HTTP code (0, 200, ...): the
    # digits of the code alone would satisfy `str(total) in text`.
    shown_total = extract_count_before(status_text, "листьев")
    assert shown_total == data["total"], (
        f"status text claims {shown_total} листьев, GET /dial says total={data['total']}: "
        f"{status_text!r}")


@test("no_horizontal_scroll_desktop")
def test_no_horizontal_scroll_desktop(browser: Browser, base_url: str):
    browser.set_device(1440, 900)
    browser.goto(f"{base_url}/ui")
    browser.wait_for("document.querySelectorAll('nav.tabs button').length > 0", timeout=10)
    scroll_width = browser.evaluate("document.documentElement.scrollWidth")
    inner_width = browser.evaluate("window.innerWidth")
    assert scroll_width <= inner_width, (
        f"document.documentElement.scrollWidth {scroll_width} > window.innerWidth {inner_width} "
        f"at 1440px — page scrolls sideways")


@test("pager_advances")
def test_pager_advances(browser: Browser, base_url: str):
    problems = []
    for tab in PAGE_TABS:
        browser.goto(f"{base_url}/ui#{tab}")
        browser.wait_for("document.querySelector('#main .pager') !== null", timeout=10)
        before_text = browser.evaluate("document.querySelector('#main .pager').textContent")
        from0, to0, total0 = parse_pager(before_text)
        if from0 != 1 or total0 <= to0:
            # Not a skip: a stand too thin to page forward makes this test meaningless,
            # and that has to be loud, not a quiet pass.
            raise AssertionError(f"[{tab}] stand has too little data to page forward "
                                  f"({before_text!r}) — pager_advances needs more than one page")
        page_size = to0 - from0 + 1   # read off the pager, not the PAGE constant copied in
        expect_from, expect_to = from0 + page_size, min(to0 + page_size, total0)
        server_pattern = f"GET /{tab}?limit={page_size}&offset={page_size}"
        before_hits = browser.count_requests(server_pattern)

        click_pager_forward(browser, before_text)

        after_text = browser.evaluate("document.querySelector('#main .pager').textContent")
        from1, to1, total1 = parse_pager(after_text)
        after_hits = browser.count_requests(server_pattern)

        if (from1, to1) != (expect_from, expect_to):
            problems.append(f"[{tab}] expected {expect_from}–{expect_to} after "
                             f"'вперёд →', got {from1}–{to1} ({after_text!r})")
        if total1 != total0:
            problems.append(f"[{tab}] total changed across a page turn: {total0} -> {total1}")
        if after_hits <= before_hits:
            problems.append(f"[{tab}] server access log shows no '{server_pattern}' "
                             f"(had {before_hits} before the click, {after_hits} after)")
    assert not problems, "; ".join(problems)


@test("pager_holds_offset_on_failure")
def test_pager_holds_offset_on_failure(browser: Browser, base_url: str):
    """§4.1's actual bug: `state.offset` used to get written before the request that was
    supposed to justify it had even come back, so three failed 'вперёд' clicks in a row
    left the page silently reading from offset=75 with rows from offset=0 on screen. A
    failed click must leave both the SCREEN and the next click's request right where they
    started; only a second, successful click may move either one.

    Parametrized over all three PAGE_TABS: the fix landed in `load()`, which is
    duplicated per view (`state.offset` is a separate closure variable in each of
    "Идеи"/"Тезисы"/"Источники"), so a guard proven only on "Идеи" says nothing about the
    other two — proven directly: moving the assignment back up in "Тезисы" alone left this
    test green when it only ever drove "Идеи"."""
    problems = []
    for tab in PAGE_TABS:
        browser.goto(f"{base_url}/ui#{tab}")
        browser.wait_for("document.querySelector('#main .pager') !== null", timeout=10)
        before_text = browser.evaluate("document.querySelector('#main .pager').textContent")
        from0, to0, total0 = parse_pager(before_text)
        if not (from0 == 1 and total0 > to0):
            problems.append(f"[{tab}] stand has too little data to page forward "
                             f"({before_text!r}) — this tab was not actually tested")
            continue

        # Break exactly the requests this click makes to /{tab} (not /ideas/{id}, which
        # the detail card also uses) — everything else passes through untouched.
        browser.evaluate("""
          (() => {
            window.__e2eOrigFetch = window.fetch;
            window.fetch = function(input, init) {
              const url = typeof input === "string" ? input : (input && input.url) || "";
              if (url.includes(%s)) return Promise.reject(new TypeError("e2e-injected: offline"));
              return window.__e2eOrigFetch(input, init);
            };
          })()
        """ % json.dumps(f"/{tab}?"))
        try:
            browser.click("#main .pager button:nth-child(2)")
            browser.wait_for("document.querySelector('#main .status.err') !== null", timeout=10)
        finally:
            # Always restored, even on assertion failure below — a leaked patch here would
            # silently break every later test that touches the network (though each test
            # now gets a fresh document via goto()'s _t= param regardless).
            browser.evaluate("(() => { if (window.__e2eOrigFetch) { "
                              "window.fetch = window.__e2eOrigFetch; delete window.__e2eOrigFetch; } })()")

        stalled_text = browser.evaluate("document.querySelector('#main .pager').textContent")
        from_s, to_s, total_s = parse_pager(stalled_text)
        if (from_s, to_s) != (from0, to0):
            problems.append(f"[{tab}] a FAILED 'вперёд' already moved the pager to "
                             f"{from_s}–{to_s}; it should still read {from0}–{to0} "
                             f"({stalled_text!r})")
            continue

        click_pager_forward(browser, stalled_text)
        final_text = browser.evaluate("document.querySelector('#main .pager').textContent")
        from1, to1, total1 = parse_pager(final_text)
        page_size = to0 - from0 + 1
        expect_from, expect_to = from0 + page_size, min(to0 + page_size, total0)
        if (from1, to1) != (expect_from, expect_to):
            problems.append(f"[{tab}] after RETRYING 'вперёд', pager shows {from1}–{to1}, "
                             f"expected {expect_from}–{expect_to} — the failed click must "
                             f"not have made the retry skip a page it never actually "
                             f"fetched ({final_text!r})")
    assert not problems, "; ".join(problems)


@test("pager_holds_offset_on_wrong_shape")
def test_pager_holds_offset_on_wrong_shape(browser: Browser, base_url: str):
    """`pager_holds_offset_on_failure` only ever breaks the request itself (`Promise.reject`,
    thrown before `state.offset = offset` runs at all) and `wrong_shape_200_is_an_error_not_a_spinner`
    only ever checks that a wrong-shape 200 ends in an error status, never reading the pager
    afterwards. Between them the actual §4.1 bug — a 200 that resolves `api()` successfully
    with a body of the wrong shape, which used to let `state.offset = offset` commit BEFORE
    the shape check that now runs first — went unexercised by either half. Same three tabs,
    same 'вперёд →' twice pattern as `pager_holds_offset_on_failure`, but the injected fault
    is a 200 of the wrong shape instead of a rejected fetch, and the check is that a SECOND,
    real 'вперёд →' after the fault lands on the very next page (26–50), not one page further
    on (51–75) the way a silently-committed offset from the first click would produce."""
    problems = []
    for tab in PAGE_TABS:
        browser.goto(f"{base_url}/ui#{tab}")
        browser.wait_for("document.querySelector('#main .pager') !== null", timeout=10)
        before_text = browser.evaluate("document.querySelector('#main .pager').textContent")
        from0, to0, total0 = parse_pager(before_text)
        if not (from0 == 1 and total0 > to0):
            problems.append(f"[{tab}] stand has too little data to page forward "
                             f"({before_text!r}) — this tab was not actually tested")
            continue

        # Answers exactly the NEXT request to /{tab}? with a 200 of the wrong shape, then
        # falls back to the real fetch — a one-shot fault, not a permanent one, so the
        # second 'вперёд →' below hits the real API.
        browser.evaluate("""
          (() => {
            window.__e2eOrigFetch = window.fetch;
            let used = false;
            window.fetch = function(input, init) {
              const url = typeof input === "string" ? input : (input && input.url) || "";
              if (!used && url.includes(%s)) {
                used = true;
                return Promise.resolve(new Response(JSON.stringify({surprise: "not a page"}),
                  {status: 200, headers: {"Content-Type": "application/json"}}));
              }
              return window.__e2eOrigFetch(input, init);
            };
          })()
        """ % json.dumps(f"/{tab}?"))
        try:
            browser.click("#main .pager button:nth-child(2)")
            browser.wait_for("document.querySelector('#main .status.err') !== null", timeout=10)
        finally:
            browser.evaluate("(() => { if (window.__e2eOrigFetch) { "
                              "window.fetch = window.__e2eOrigFetch; delete window.__e2eOrigFetch; } })()")

        stalled_text = browser.evaluate("document.querySelector('#main .pager').textContent")
        from_s, to_s, total_s = parse_pager(stalled_text)
        if (from_s, to_s) != (from0, to0):
            problems.append(f"[{tab}] a wrong-shape 200 already moved the pager to "
                             f"{from_s}–{to_s}; it should still read {from0}–{to0} "
                             f"({stalled_text!r})")
            continue

        click_pager_forward(browser, stalled_text)
        final_text = browser.evaluate("document.querySelector('#main .pager').textContent")
        from1, to1, total1 = parse_pager(final_text)
        page_size = to0 - from0 + 1
        expect_from, expect_to = from0 + page_size, min(to0 + page_size, total0)
        if (from1, to1) != (expect_from, expect_to):
            problems.append(f"[{tab}] after a REAL 'вперёд' following the wrong-shape 200, "
                             f"pager shows {from1}–{to1}, expected {expect_from}–{expect_to} "
                             f"— the wrong-shape click must not have committed an offset it "
                             f"never actually rendered ({final_text!r})")
    assert not problems, "; ".join(problems)


@test("pager_stops_at_the_end")
def test_pager_stops_at_the_end(browser: Browser, base_url: str):
    """§4.1's other half: with no guard on the last page, 'вперёд →' stays clickable past
    the end — the pager would then draw an inverted range over zero rows (e.g. "показано
    876–859 из 859"), a number on screen that is flatly wrong, which this project's own
    rules ban outright. Uses a single idea's theses (small, real, no mocked fetch) so the
    whole result fits on one page and both boundary buttons must be disabled together."""
    idea_id, _ = find_small_theses_idea(base_url)
    q = urllib.parse.quote(idea_id)
    with urllib.request.urlopen(f"{base_url}/theses?idea_id={q}&limit=25&offset=0", timeout=10) as r:
        server_total = json.load(r)["total"]
    assert 1 <= server_total <= 25, (
        f"idea {idea_id} has {server_total} theses server-side — does not fit one page, "
        f"find_small_theses_idea should not have picked it")

    browser.goto(f"{base_url}/ui#theses")
    browser.wait_for("document.querySelector('#main .pager') !== null", timeout=10)
    # The unfiltered load's pager is already in the DOM at this point (thousands of
    # theses) — waiting only for "a pager exists" would pass on THAT stale pager the
    # instant the filtered fetch is still in flight, not on the filtered result.
    unfiltered_text = browser.evaluate("document.querySelector('#main .pager').textContent")
    browser.fill("#main input.mono[placeholder='idea_…']", idea_id)
    click_button_with_text(browser, "Фильтровать")
    browser.wait_for("(() => { const p = document.querySelector('#main .pager'); "
                      "return p && p.textContent !== %s; })()" % json.dumps(unfiltered_text),
                      timeout=10)

    pager_text = browser.evaluate("document.querySelector('#main .pager').textContent")
    from0, to0, total0 = parse_pager(pager_text)
    assert (from0, to0, total0) == (1, server_total, server_total), (
        f"filtered to idea {idea_id}: expected 1–{server_total} из {server_total}, "
        f"got {pager_text!r}")

    back_disabled, fwd_disabled = browser.evaluate(
        "[...document.querySelectorAll('#main .pager button')].map((b) => b.disabled)")
    assert back_disabled, "'← назад' is not disabled on a single-page result"
    assert fwd_disabled, (
        "'вперёд →' is not disabled on the last (and only) page — an operator could click "
        "past the end and the pager would commit offset past total, drawing an inverted "
        "range over zero rows")


@test("status_code_from_server")
def test_status_code_from_server(browser: Browser, base_url: str):
    browser.goto(f"{base_url}/ui#ideas")
    browser.wait_for("document.querySelector('#main .status .code') !== null", timeout=10)
    displayed = browser.evaluate("document.querySelector('#main .status .code').textContent").strip()

    log = browser.server_log()
    lines = [l for l in log.splitlines() if re.search(r'"GET /ideas\?limit=\d+&offset=0 HTTP/1\.\d"', l)]
    assert lines, "no 'GET /ideas?limit=...&offset=0' request found in the server's access log"
    m = re.search(r'"\s+(\d{3})\s', lines[-1])
    assert m, f"couldn't find a status code in the access-log line: {lines[-1]!r}"
    real_code = m.group(1)
    assert displayed == real_code, (
        f".status .code shows {displayed!r}; the server's own access log says the last "
        f"matching GET /ideas answered {real_code!r} ({lines[-1]!r})")

    # This stand only ever answers 200 to a healthy GET (every route here defaults to
    # FastAPI's implicit 200; nothing exercised by the console returns any other 2xx),
    # so the end-to-end check above cannot by itself tell "reads the real status" apart
    # from "always prints 200" — both give displayed == real_code == "200" regardless.
    # `codeOf` and its `HTTP_STATUS` symbol are top-level `const`s in console.html's
    # inline, non-module <script>, which puts them in the page's global lexical scope —
    # visible to Runtime.evaluate in the same execution context (confirmed by hand
    # against this exact page before writing this assertion). Feeding it a synthetic
    # non-200 status closes the gap a real request cannot.
    synthetic = browser.evaluate(
        "(() => { const o = {}; o[HTTP_STATUS] = 503; return codeOf(o); })()")
    assert synthetic == 503, (
        f"codeOf() does not read the real HTTP status off a response: fed a synthetic "
        f"503, got {synthetic!r} back")

    # The synthetic probe above proves `codeOf()` itself works — it says nothing about
    # the CALL SITE in "Идеи" (`setStatus(statusHost, "ok", codeOf(page), ...)`), which a
    # hardcoded `200` literal there would satisfy identically, since this stand only ever
    # answers 200 to a healthy GET. Routes a real 201 through the actual render path
    # instead: mock exactly one `/ideas?` response, click "Обновить список", and read the
    # displayed code off the DOM the same way an operator would see it.
    with urllib.request.urlopen(f"{base_url}/ideas?limit=1&offset=0", timeout=10) as r:
        real_ideas_body = r.read().decode()
    browser.evaluate("""
      (() => {
        window.__e2eOrigFetch = window.fetch;
        let used = false;
        window.fetch = function(input, init) {
          const url = typeof input === "string" ? input : (input && input.url) || "";
          if (!used && url.includes("/ideas?")) {
            used = true;
            return Promise.resolve(new Response(%s, {status: 201,
              headers: {"Content-Type": "application/json"}}));
          }
          return window.__e2eOrigFetch(input, init);
        };
      })()
    """ % json.dumps(real_ideas_body))
    try:
        click_button_with_text(browser, "Обновить список")
        browser.wait_for(
            "document.querySelector('#main .status .code')?.textContent.trim() === '201'",
            timeout=10)
    finally:
        browser.evaluate("(() => { if (window.__e2eOrigFetch) { "
                          "window.fetch = window.__e2eOrigFetch; delete window.__e2eOrigFetch; } })()")
    real_path_displayed = browser.evaluate(
        "document.querySelector('#main .status .code').textContent").strip()
    assert real_path_displayed == "201", (
        f"a real 201 response through the actual render path shows as "
        f"{real_path_displayed!r} in #main .status .code, want '201' — the call site is "
        f"not reading the response's real status")


@test("boot_probe_network_failure_is_offline")
def test_boot_probe_offline(browser: Browser, base_url: str):
    """§4.1: no answer at all from /healthz is neither "open" nor "needs a key" — it must
    land on AUTH="offline" with the key gate staying shut, never on "need-key"."""
    ident = _install_healthz_override(browser, _reject_js())
    try:
        browser.goto(f"{base_url}/ui#ideas")
        browser.wait_for("typeof AUTH !== 'undefined' && AUTH !== 'probing'", timeout=10)
        auth = browser.evaluate("AUTH")
        assert auth == "offline", f"expected AUTH == 'offline' on a network failure, got {auth!r}"
        gate_hidden = browser.evaluate(
            "document.querySelector('#gate').classList.contains('hide')")
        assert gate_hidden, "the key gate opened on a network failure — that is not a key problem"
        text = status_text_sans_code(browser)
        assert "нет связи с API" in text, (
            f"expected 'нет связи с API' somewhere in #main's status, got {text!r}")
    finally:
        browser.remove_init_script(ident)


@test("boot_probe_503_is_broken_not_a_key_problem")
def test_boot_probe_503(browser: Browser, base_url: str):
    """§4.1: a 503 from /healthz is app.py's "lake broken" signal, never a key problem —
    must land on AUTH="broken" with the key gate staying shut."""
    ident = _install_healthz_override(browser, _response_js(503, {"error": "lake unavailable"}))
    try:
        browser.goto(f"{base_url}/ui#ideas")
        browser.wait_for("typeof AUTH !== 'undefined' && AUTH !== 'probing'", timeout=10)
        auth = browser.evaluate("AUTH")
        assert auth == "broken", f"expected AUTH == 'broken' on a 503, got {auth!r}"
        gate_hidden = browser.evaluate(
            "document.querySelector('#gate').classList.contains('hide')")
        assert gate_hidden, "the key gate opened on a 503 — that is not a key problem"
        # The code lives in its own `.code` span — `status_text_sans_code` strips it out
        # on purpose (see its docstring) so a count in the message can't be confused with
        # the HTTP code; read it back separately here instead.
        code = browser.evaluate(
            "document.querySelector('#main .status .code')?.textContent ?? ''").strip()
        assert code == "503", f"expected #main .status .code to read '503', got {code!r}"
        text = status_text_sans_code(browser)
        assert "ОЗЕРО НЕДОСТУПНО" in text, (
            f"expected the 'ОЗЕРО НЕДОСТУПНО' explanation in #main, got {text!r}")
    finally:
        browser.remove_init_script(ident)


@test("boot_probe_401_opens_gate_with_real_text")
def test_boot_probe_401(browser: Browser, base_url: str):
    """§4.1's third bug: AUTH only reaches "need-key" from a real 401, and the gate must
    open with api()'s actual explanation, not a blank or a generic fallback string that
    could clobber it."""
    ident = _install_healthz_override(browser, _response_js(401, {"error": "unauthorized"}))
    try:
        browser.goto(f"{base_url}/ui#ideas")
        browser.wait_for("typeof AUTH !== 'undefined' && AUTH !== 'probing'", timeout=10)
        auth = browser.evaluate("AUTH")
        assert auth == "need-key", f"expected AUTH == 'need-key' on a 401, got {auth!r}"
        gate_open = browser.evaluate(
            "!document.querySelector('#gate').classList.contains('hide')")
        assert gate_open, "the key gate did not open on a 401"
        msg = browser.evaluate("document.querySelector('#key-msg').textContent").strip()
        assert msg, "the key gate opened with no explanation text (#key-msg is empty)"
    finally:
        browser.remove_init_script(ident)


@test("tabs_render_before_probe_resolves")
def test_tabs_render_before_probe_resolves(browser: Browser, base_url: str):
    """§7.6: tabs and an explanation belong on screen immediately, not after a round trip
    to /healthz. Proven by making that round trip slow (1.5s) and reading the DOM while it
    is still in flight — `goto()` only waits for readyState=="complete", which this page
    reaches long before an async fetch with a 1.5s artificial delay settles."""
    ident = _install_healthz_override(browser, _response_js(200, {"status": "ok"}, delay_ms=1500))
    try:
        browser.goto(f"{base_url}/ui#ideas")
        auth_mid_flight = browser.evaluate("AUTH")
        tabs_now = browser.evaluate("document.querySelectorAll('nav.tabs button').length")
        assert auth_mid_flight == "probing", (
            f"the probe already resolved before this check ran (AUTH={auth_mid_flight!r}) "
            f"— this assertion needs to catch it mid-flight, tighten the delay")
        assert tabs_now > 0, "nav.tabs has no buttons while the boot probe is still in flight"
        browser.wait_for("AUTH === 'open'", timeout=10)   # let the delayed probe finish cleanly
    finally:
        browser.remove_init_script(ident)


@test("write_confirmation_survives_the_reread")
def test_write_confirmation_survives_the_reread(browser: Browser, base_url: str):
    """A "записано" confirmation belongs to whichever idea was actually written, not to
    the next one someone happens to open. Mocks only the PATCH response (200) — every GET,
    including the re-read `show(ideaId)` triggers right after, passes through to the real
    stand; this project bans actually writing to the graph from a test."""
    with urllib.request.urlopen(f"{base_url}/ideas?limit=2&offset=0", timeout=10) as r:
        ideas = json.load(r)["items"]
    assert len(ideas) >= 2, "stand needs at least two ideas for this test"
    idea_a, idea_b = ideas[0]["id"], ideas[1]["id"]

    browser.goto(f"{base_url}/ui#ideas")
    browser.wait_for("document.querySelector(\"#main input.mono[placeholder='idea_…']\") !== null",
                      timeout=10)
    browser.fill("#main input.mono[placeholder='idea_…']", idea_a)
    click_button_with_text(browser, "Открыть")
    browser.wait_for(
        "[...document.querySelectorAll('#main h2')].some((h) => h.textContent.includes('PATCH /ideas'))",
        timeout=10)

    browser.evaluate("""
      (() => {
        window.__e2eOrigFetch = window.fetch;
        window.fetch = function(input, init) {
          const method = (init && init.method) || "GET";
          if (method === "PATCH") {
            return Promise.resolve(new Response(JSON.stringify({updated_at: "e2e-mock"}),
              {status: 200, headers: {"Content-Type": "application/json"}}));
          }
          return window.__e2eOrigFetch(input, init);
        };
      })()
    """)
    # Genuinely ON SCREEN, not just present in the DOM under the sticky header —
    # `writeStatus`'s own `scroll-margin-top:110px` names exactly this trap. Reused for
    # both the poll below and the final read: the click handler sets the confirmation
    # SYNCHRONOUSLY, then fires `show(ideaId)` (a real, unmocked re-read of the idea) and
    # only THAT re-read's tail end calls `scrollIntoView` — a one-shot check right after
    # the click would race that async scroll, same as the failure this replaced.
    probe_js = (
        "(() => { const box = [...document.querySelectorAll('#main .status')]"
        ".find((b) => b.textContent.includes('записано:')); if (!box) return null; "
        "const r = box.getBoundingClientRect(); "
        "const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2); "
        "return { onScreen: !!(hit && box.contains(hit)), text: box.textContent }; })()"
    )
    try:
        set_labeled_input(browser, "text", "e2e mutated text " + idea_a)
        click_button_with_text(browser, "Записать изменения")
        browser.wait_for(f"((v) => v && v.onScreen)({probe_js})", timeout=10)
    finally:
        browser.evaluate("(() => { if (window.__e2eOrigFetch) { "
                          "window.fetch = window.__e2eOrigFetch; delete window.__e2eOrigFetch; } })()")

    visible = browser.evaluate(probe_js)
    assert visible, "write confirmation not found in #main after a mocked-200 PATCH"
    assert visible["onScreen"], (
        f"write confirmation exists in the DOM but is covered by something else on "
        f"screen (likely the sticky header): {visible['text']!r}")

    # A DIFFERENT idea must not carry idea A's confirmation onto its own card.
    browser.fill("#main input.mono[placeholder='idea_…']", idea_b)
    click_button_with_text(browser, "Открыть")
    browser.wait_for(
        "[...document.querySelectorAll('#main h2')].some((h) => h.textContent === "
        + json.dumps("идея " + idea_b) + ")", timeout=10)
    still_there = browser.evaluate(
        "[...document.querySelectorAll('#main .status')].some((b) => "
        "b.textContent.includes('записано:'))")
    assert not still_there, "idea A's write confirmation is still showing after opening idea B"

    # "Закрыть карточку" is only added once idea B's card finishes rendering (edges +
    # form, both behind an await) — the h2 above appears synchronously, well before it.
    browser.wait_for(
        "[...document.querySelectorAll('#main button')].some((b) => "
        "b.textContent.trim() === " + json.dumps("Закрыть карточку") + ")", timeout=10)
    click_button_with_text(browser, "Закрыть карточку")
    still_there_2 = browser.evaluate(
        "[...document.querySelectorAll('#main .status')].some((b) => "
        "b.textContent.includes('записано:'))")
    assert not still_there_2, "write confirmation is still showing after 'Закрыть карточку'"


def _install_shape_override(browser: Browser, url_substring: str) -> None:
    """Patches `window.fetch` on the CURRENT document so any request whose URL contains
    `url_substring` answers 200 with a body of the wrong shape (`{"surprise": ...}`,
    no `items`/`hits` a view could ever expect) — everything else passes through. Uses
    `evaluate` (not `add_init_script`): installed AFTER a tab's own baseline load has
    already run for real, so the mock only catches the deliberate refresh this test
    fires next, not the page's initial GET. Not restored on return — every call site
    below is the last thing that touches its tab before the next `goto()` replaces the
    whole document anyway (see `Browser.goto`'s docstring)."""
    browser.evaluate("""
      (() => {
        const orig = window.fetch;
        window.fetch = function(input, init) {
          const url = typeof input === "string" ? input : (input && input.url) || "";
          if (url.includes(%s)) {
            return Promise.resolve(new Response(JSON.stringify({surprise: "not a page"}),
              {status: 200, headers: {"Content-Type": "application/json"}}));
          }
          return orig(input, init);
        };
      })()
    """ % json.dumps(url_substring))


@test("wrong_shape_200_is_an_error_not_a_spinner")
def test_wrong_shape_200_is_an_error_not_a_spinner(browser: Browser, base_url: str):
    """run()'s catch (console.html:367-377) is the ONLY thing standing between a 200 with
    a body of the wrong shape (captive portal, transparent proxy, a route that changed
    under the page) and a spinner that never resolves — a render-time TypeError from code
    like `page.items.map` is not an ApiError, and reverting run() to only catch ApiError
    and rethrow everything else (the mutation this test exists to catch) leaves the
    promise rejected with nothing downstream to answer it: no status update, spinner stuck
    forever, no test anywhere fed the page a 200 of the wrong shape to notice. Checked on
    every list tab separately (ideas/theses/sources auto-load through `run()` in `load()`,
    search through `fire()`) because each view's OWN rendering code is what actually throws
    — a guard proven on one view says nothing about whether another view's render path
    even reaches a throw before it touches `.length` on the wrong thing."""
    configs = [
        ("ideas", "/ideas?", lambda: click_button_with_text(browser, "Обновить список")),
        ("theses", "/theses?", lambda: click_button_with_text(browser, "Фильтровать")),
        ("sources", "/sources?", lambda: click_button_with_text(browser, "Обновить")),
        ("search", "/search?", lambda: click_button_with_text(browser, "Искать")),
    ]
    problems = []
    for tab, url_substring, trigger in configs:
        try:
            browser.goto(f"{base_url}/ui#{tab}")
            if tab == "search":
                browser.wait_for("document.querySelector('#main input.grow') !== null", timeout=10)
                browser.fill("#main input.grow", "wrong shape e2e probe")
                click_button_with_text(browser, "Искать")
                browser.wait_for("document.querySelector('#main .status.ok') !== null", timeout=15)
            else:
                browser.wait_for("document.querySelector('#main .pager') !== null", timeout=10)

            _install_shape_override(browser, url_substring)
            trigger()
            browser.wait_for("document.querySelector('#main .status.err') !== null", timeout=10)

            spinner = browser.evaluate("document.querySelector('#main .spinner') !== null")
            if spinner:
                problems.append(f"[{tab}] a spinner is still in the DOM after the wrong-shape "
                                 f"200 settled into an error status")

            text = status_text_sans_code(browser)
            if "нет связи с api" in text.lower():
                problems.append(f"[{tab}] status text blames the network ('нет связи с API') "
                                 f"for a response that actually arrived: {text!r}")
            if "форм" not in text.lower():
                problems.append(f"[{tab}] status text does not name the response's SHAPE as "
                                 f"the problem: {text!r}")
        except Exception as exc:
            problems.append(f"[{tab}] {exc}")
    assert not problems, "; ".join(problems)


@test("raw_tab_shows_only_its_own_answer")
def test_raw_tab_shows_only_its_own_answer(browser: Browser, base_url: str):
    """§'Ручки': the raw-call tab renders path, code and body as three separate reads of
    the SAME answer — `out.replaceChildren()` (console.html, right before `run()` fires
    the request) is the only thing that stops a later FAILED call from leaving an earlier
    call's body on screen under the new call's code. Proven directly: a real /healthz
    (200, body rendered), then a real 404 on a route that does not exist — the badge must
    read 404 and the pane below it must no longer hold /healthz's body. The badge is also
    cross-checked against the server's own access log, not just against what the page
    claims — the point of this tab is exactly "the ops handle, no translation layer"."""
    browser.goto(f"{base_url}/ui#raw")
    browser.wait_for("document.querySelector('#main input.mono.grow') !== null", timeout=10)

    browser.fill("#main input.mono.grow", "/healthz")
    click_button_with_text(browser, "Позвать")
    browser.wait_for("document.querySelector('#main pre.mono')?.textContent.includes('\"status\"')",
                      timeout=10)
    healthz_body = browser.evaluate("document.querySelector('#main pre.mono').textContent")
    assert '"status"' in healthz_body, f"/healthz body did not render as expected: {healthz_body!r}"

    missing_path = "/e2e-no-such-route-wrong-tab-body"
    browser.fill("#main input.mono.grow", missing_path)
    click_button_with_text(browser, "Позвать")
    browser.wait_for(
        "document.querySelector('#main .status .code')?.textContent.trim() === '404'", timeout=10)

    displayed_code = browser.evaluate(
        "document.querySelector('#main .status .code').textContent").strip()
    assert displayed_code == "404", (
        f"#main .status .code shows {displayed_code!r} after calling a route that does not "
        f"exist, want '404'")

    log = browser.server_log()
    lines = [l for l in log.splitlines()
             if re.search(rf'"GET {re.escape(missing_path)} HTTP/1\.\d"', l)]
    assert lines, f"no 'GET {missing_path}' request found in the server's access log"
    m = re.search(r'"\s+(\d{3})\s', lines[-1])
    assert m, f"couldn't find a status code in the access-log line: {lines[-1]!r}"
    real_code = m.group(1)
    assert displayed_code == real_code, (
        f"#main .status .code shows {displayed_code!r}; the server's own access log says "
        f"the last matching GET {missing_path} answered {real_code!r} ({lines[-1]!r})")

    body_after_404 = browser.evaluate("document.querySelector('#main pre.mono')")
    assert body_after_404 is None, (
        "a <pre.mono> is still in #main after a FAILED call (404) — it must be /healthz's "
        "leftover body, since the failure path itself never renders one; out.replaceChildren() "
        "before the request fires is what is supposed to prevent this")


@test("single_request_on_double_click", expected_fail=True)
def test_single_request_on_double_click(browser: Browser, base_url: str):
    """No debounce/disable on the dial's 'Разложить' button yet — a second click before
    the first answer lands fires a second GET /dial. Left registered and expected to
    fail so the guard, once it ships, has to make this test start passing rather than
    quietly existing unverified (see `test()` docstring for the XFAIL/XPASS contract)."""
    hypothesis = "guard the second click before the first answer lands"
    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    browser.fill("#main textarea", hypothesis)

    before = browser.count_requests("GET /dial?")
    browser.click("#main button.act")
    browser.click("#main button.act")
    browser.wait_for("document.querySelector('.graphwrap svg') !== null", timeout=20)
    time.sleep(0.5)   # let a second in-flight request, if the page fired one, finish and log
    made = browser.count_requests("GET /dial?") - before

    assert made == 1, (
        f"double-clicking 'Разложить' fired {made} GET /dial requests, want 1 — no guard "
        f"against a second click before the first answer lands yet")


@test("phone_no_horizontal_scroll", expected_fail=True)
def test_phone_no_horizontal_scroll(browser: Browser, base_url: str):
    """§0.3: the page runs off the side of a phone screen today, and
    `document.documentElement.scrollWidth` alone is blind to nearly all of it — see
    `measure_overflow`'s docstring. Measured by hand at 390x844 before writing this:
    nav.tabs clientWidth 390 / scrollWidth 785, #ideas .scroller 352 / 713, header.bar
    bottom ~103px against the CSS-hardcoded `nav.tabs{top:53px}`, and there is no
    `@media` rule under 620px or 430px anywhere in the file. Expected to fail until
    §0.3/§2.3 land — see `test()` docstring for what XFAIL/XPASS mean here. Drop
    `expected_fail=True` the SAME COMMIT that fixes the phone layout: an XFAIL that starts
    passing and nobody notices is exactly the empty guarantee this suite exists to avoid."""
    browser.set_device(390, 844, mobile=True, touch=True)
    problems = []
    for tab_id in ("retrieve", "dial", "search", "ideas", "theses", "sources",
                   "ingest", "graph", "raw"):
        browser.goto(f"{base_url}/ui#{tab_id}")
        browser.wait_for("document.querySelector('#main .panel') !== null", timeout=10)

        overflow = measure_overflow(browser)
        if overflow["docOverflow"]:
            problems.append(f"[{tab_id}] document scrollWidth {overflow['docScrollWidth']} "
                             f"> innerWidth {overflow['innerWidth']}")
        problems.extend(f"[{tab_id}] {e}" for e in overflow["elements"])

        overlap = header_nav_overlap(browser)
        if overlap and overlap["headerBottom"] > overlap["navTop"] + 1:
            problems.append(f"[{tab_id}] header.bar bottom {overlap['headerBottom']:.0f}px "
                             f"is past nav.tabs' hardcoded top {overlap['navTop']:.0f}px — "
                             f"the tab strip is drawn under the header, not below it")
    assert not problems, "phone (390x844) overflows sideways: " + "; ".join(problems)


# ================================================================================ main

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="run a single test by name")
    parser.add_argument("--keep", action="store_true",
                        help="leave the server and Chrome running on exit")
    parser.add_argument("--full", action="store_true",
                        help="refuse to run on --mock: exit nonzero instead of silently "
                             "skipping /retrieve, /research, /fetch and /run because the "
                             "standing server on :8077 holds writer.lock")
    args = parser.parse_args(argv)

    names = {n for n, _, _ in TESTS}
    if args.only and args.only not in names:
        print(f"unknown test: {args.only!r} (have: {', '.join(sorted(names))})")
        return 1

    mock = not writer_lock_free()
    if args.full and mock:
        # A silent fallback to --mock is exactly the fail-open this project's rules ban —
        # --full exists so a run can insist on the real thing instead of degrading quietly.
        print("--full given, but writer.lock is held (likely the standing server on "
              ":8077) — stop that server so this run can hold the lock itself, then "
              "rerun with --full for an honest pass over /retrieve, /research, /fetch "
              "and /run.")
        return 1
    if mock:
        # Degrading silently is exactly the fail-open this project's own rules ban —
        # say on stdout, in its own line, which paths this run could not touch.
        print("writer.lock is held (likely the standing server on :8077) — falling back "
              "to --mock for this run; /retrieve, /research, /fetch, /run and "
              "/ingest/phase2 were NOT exercised")

    scratch = Path(tempfile.mkdtemp(prefix="e2e_console_"))
    (scratch / "chrome-profile").mkdir()
    server = LakeServer(free_port(), scratch / "server.log")
    chrome = Chrome(free_port(), scratch / "chrome-profile")
    browser = None
    passed = xfailed = failures = 0
    run = [(n, f, xf) for n, f, xf in TESTS if not args.only or n == args.only]

    try:
        server.start(mock)
        server.wait_ready()
        chrome.start()
        chrome.wait_ready()
        browser = Browser(chrome.port, log_path=server.log_path)

        for name, fn, xfail in run:
            browser.clear_device()   # one test's set_device() must never leak into the next
            try:
                fn(browser, server.base_url)
            except Exception as exc:
                if xfail:
                    xfailed += 1
                    print(f"XFAIL {name}: {exc}")
                else:
                    failures += 1
                    print(f"FAIL {name}: {exc}")
                    traceback.print_exc()
            else:
                if xfail:
                    # An XFAIL that starts passing is news, not a bonus: the guard it was
                    # standing in for either shipped (drop expected_fail=True) or the test
                    # rotted into not testing anything — either way this must fail the run,
                    # not silently count toward "passed".
                    failures += 1
                    print(f"XPASS {name} (marked expected_fail but passed — "
                          f"the guard shipped, drop expected_fail=True)")
                else:
                    passed += 1
                    print(f"PASS {name}")
    finally:
        if args.keep:
            print(f"--keep: server {server.base_url} (log: {server.log_path}), "
                  f"chrome devtools 127.0.0.1:{chrome.port}, scratch {scratch}")
        else:
            if browser:
                browser.close()
            chrome.stop()
            server.stop()

    # `len(run) - failures` used to double as "passed", which silently counted every
    # XFAIL as a pass (an XFAIL raises, same as a FAIL, but was never in `failures`) —
    # "7/8 passed" on a run that genuinely passed 6. Three separate buckets instead.
    print(f"{passed} passed, {xfailed} xfail, {failures} failed (of {len(run)})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
