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
import math
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
    mouse_wheel(x, y, dx=, dy=)       Input.dispatchMouseEvent type "mouseWheel" — a real
                                      scroll gesture, distinct from writing scrollLeft (see
                                      its own docstring for why that distinction matters).
    click(selector)                   Real mouse: center of the element, dispatchMouseEvent.
    press(selector, key)              Real keyboard: dispatchKeyEvent (e.g. "Enter").
    fill(selector, text)              Sets .value, dispatches input+change.
    screenshot(path)                  Page.captureScreenshot, full page height.
    console_messages() -> list        Browser-console messages since the last goto() —
                                      [{"type": "log"/"error"/"exception"/..., "text": str}].
    wait_for_dialog(timeout=10)       Blocks until a confirm()/alert() opens, returns its
                                      CDP params (has "message"); does not answer it.
    handle_dialog(accept, prompt_text="")  Answers the most recent open dialog.
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
        # Browser-console messages, oldest first: {"type": "log"/"error"/..., "text": str}.
        # Populated from Runtime.consoleAPICalled (console.log/warn/error/assert/...) AND
        # Runtime.exceptionThrown (an uncaught throw, which selftest's own `assert` helper
        # may use instead of console.error) — a test that only watched one of the two would
        # miss whichever failure shape the page actually picked. Cleared on every goto()
        # (see below) so each test reads only its own document's messages.
        self._console = []
        # window.confirm()/alert() dialogs seen since the last goto(), oldest first — a
        # dialog PAUSES page JS until `Page.handleJavaScriptDialog` answers it, so this is
        # populated straight off the CDP event (`Page.javascriptDialogOpening`) rather than
        # by polling a JS global, which would itself be blocked by the very pause it's
        # trying to observe. See `wait_for_dialog`/`handle_dialog` below.
        self._dialogs = []
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
        # Command responses ({"id": ...}) resolve a pending Future; the two console-facing
        # events (no "id") are appended to self._console for console_messages() to read —
        # everything else (Page.* events, etc.) is still ignored, no test here needs it.
        async for raw in self._ws:
            msg = json.loads(raw)
            if "id" not in msg:
                method = msg.get("method")
                if method == "Runtime.consoleAPICalled":
                    p = msg.get("params", {})
                    args = p.get("args", [])
                    text = " ".join(str(a.get("value", a.get("description", ""))) for a in args)
                    self._console.append({"type": p.get("type", "log"), "text": text})
                elif method == "Runtime.exceptionThrown":
                    p = msg.get("params", {})
                    detail = p.get("exceptionDetails", {})
                    desc = (detail.get("exception") or {}).get("description") or detail.get("text") or ""
                    self._console.append({"type": "exception", "text": desc})
                elif method == "Page.javascriptDialogOpening":
                    # The page is paused right now — nothing here evaluates JS, only
                    # records the event so `wait_for_dialog` (a plain Python poll) can see
                    # it and `handle_dialog` can answer it over CDP.
                    self._dialogs.append(msg.get("params", {}))
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
        self._console.clear()   # each test's page gets its own, not the previous test's leftovers
        self._dialogs.clear()   # ditto for any confirm()/alert() left over from a previous test
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

    def tap_and_drag(self, x, y, dx=1, dy=1):
        """Like `tap`, but with one `touchMove` of `(dx, dy)` px inserted before `touchEnd` —
        a stationary `tap()` fires `pointerdown`/`pointerup` only (confirmed on the page: no
        `pointermove` at all for a motionless touch), so it cannot exercise any code gated on
        a touch `pointermove`. This is the only way to get a real, CDP-driven `pointermove`
        with `pointerType: "touch"` onto the page at all."""
        self._call("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [
            {"x": x, "y": y, "radiusX": 5, "radiusY": 5, "force": 1}]})
        time.sleep(0.05)
        self._call("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [
            {"x": x + dx, "y": y + dy, "radiusX": 5, "radiusY": 5, "force": 1}]})
        time.sleep(0.05)
        self._call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    def mouse_wheel(self, x, y, delta_x=0, delta_y=0):
        """A real, trusted wheel scroll (`Input.dispatchMouseEvent` type `"mouseWheel"`) at
        a viewport point — needed to tell an `overflow-x:auto` row (a user CAN scroll it)
        apart from `overflow-x:hidden` (content past the edge is gone for good). Setting
        `element.scrollLeft` directly does NOT make that distinction: Chrome still honours a
        script write to `scrollLeft` on an `overflow:hidden` container (only the user's own
        scroll gesture is blocked), so a check built on it would pass on both — only a real
        input event actually exercises the CSS property."""
        self._call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y,
                                                  "deltaX": delta_x, "deltaY": delta_y})

    def pen_tap(self, x, y):
        """A tap driven by a real digital pen — `Input.dispatchMouseEvent` with
        `pointerType: "pen"`, CDP's own primitive for this, no `Input.dispatchTouchEvent`
        involved at all (that one only ever reports `pointerType: "touch"`, never `"pen"`).
        Confirmed against the page (`event.pointerType` read back on `pointerdown`, both
        pen tests below check it) rather than assumed: Chromium tags both the `pointerdown`
        and its own synthetic `click` with `"pen"`, which is exactly the input `hasHover()`
        (console.html) must treat the same as `"touch"` — the regression this exists to
        catch shipped BECAUSE the click guard used to check `=== "touch"` and a pen's click
        (`!== "touch"`) sailed straight through it."""
        self._call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y,
                                                  "pointerType": "pen"})
        self._call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y,
                                                  "button": "left", "clickCount": 1,
                                                  "pointerType": "pen"})
        self._call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y,
                                                  "button": "left", "clickCount": 1,
                                                  "pointerType": "pen"})

    def _center_of(self, selector):
        js = ("(() => { const el = document.querySelector(%s); "
              "if (!el) throw new Error('no element for selector: ' + %s); "
              "el.scrollIntoView({block: 'center', inline: 'center'}); "
              "const r = el.getBoundingClientRect(); "
              "return {x: r.x + r.width / 2, y: r.y + r.height / 2}; })()"
              % (json.dumps(selector), json.dumps(selector)))
        return self.evaluate(js)

    def click_point(self, x, y):
        """The real-mouse half of `click()`, split out so a caller that already knows WHERE
        to click (e.g. `find_hittable_point` below, working around an SVG element occluded
        at its own geometric centre) does not have to re-resolve a selector to a point."""
        self._call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        self._call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y,
                                                  "button": "left", "clickCount": 1})
        self._call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y,
                                                  "button": "left", "clickCount": 1})

    def click(self, selector):
        pt = self._center_of(selector)
        self.click_point(pt["x"], pt["y"])

    def hover_point(self, x, y):
        """A real mouse move with no press, no release — `Input.dispatchMouseEvent` with
        `type: mouseMoved`. Chrome's own hit-testing turns this into a genuine
        `pointerType: "mouse"` `pointermove` (proven directly, not assumed — see
        `mouse_hover_shows_card_and_leave_hides_it` below, which reads `event.pointerType`
        back off the page). Split out from `click_point` because a hover-only probe (§2.3's
        "наведение показывает, увод скрывает") must never also press — a press is a click,
        and a click on an idea node is a SEPARATE, already-covered code path
        (`click_idea_node_opens_it_on_first_load`)."""
        self._call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})

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

    def console_messages(self) -> list:
        """Browser-console messages seen since the last goto(), oldest first — see
        `_read_loop`'s docstring for what feeds this. A page that never opens DevTools and
        never calls console.log still gets an empty list here, same as one that crashed
        silently; callers that need to tell "nothing happened" apart from "the guard is
        missing" must assert on CONTENT (e.g. a specific "SELFTEST OK" string), never on
        list truthiness alone."""
        return list(self._console)

    def wait_for_dialog(self, timeout=10) -> dict:
        """Blocks until a `window.confirm()`/`alert()` has opened (`Page.javascriptDialogOpening`
        already fired) and returns its CDP params (`{"message": ..., "type": "confirm"/"alert"/...,
        ...}`). The dialog itself keeps blocking page JS until `handle_dialog` answers it — this
        only observes that it opened, it does not answer it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._dialogs:
                return self._dialogs[-1]
            time.sleep(0.05)
        raise TimeoutError(f"no confirm()/alert() dialog opened within {timeout}s")

    def handle_dialog(self, accept: bool, prompt_text: str = "") -> None:
        """Answers the most recently opened dialog — `accept=False` is a real Cancel click,
        not just letting the promise reject, so a test asserting 'nothing sent' is asserting
        against the exact same user action an operator would take."""
        self._call("Page.handleJavaScriptDialog", {"accept": accept, "promptText": prompt_text})
        if self._dialogs:
            self._dialogs.pop()

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


def find_hittable_point(browser: Browser, selector: str):
    """The centre point of the FIRST element matching `selector` that a real mouse click
    would actually land on — i.e. `document.elementFromPoint` at that centre resolves back
    to the element itself, not to something drawn on top of it. Needed because the dial's
    SVG (`drawDial`) paints several purely-decorative circles with `fill="none"` (the
    cosine-gap ring, the hover ring, ring labels — the whole `gLab` group) AFTER `gIdeas` in
    z-order, and Chrome hit-tests their full interior despite the empty fill: an idea node
    that happens to land near the dial's centre gets its own bounding-box centre stolen by
    one of those overlays (confirmed directly: `elementsFromPoint` at such a node's centre
    returned two unclassed `gLab` circles ABOVE `circle.ideanode` in the hit stack). Plain
    `Browser.click(selector)` — coordinate math only, no `elementFromPoint` check — would
    silently click the WRONG thing there and this project bans exactly that kind of quiet
    lie. Returns `{x, y}` in viewport coordinates, or `None` if nothing in the set is
    reachable by a real click at all."""
    return browser.evaluate("""
      (() => {
        const nodes = [...document.querySelectorAll(%s)];
        for (const n of nodes) {
          n.scrollIntoView({block: 'center', inline: 'center'});
          const r = n.getBoundingClientRect();
          const x = r.x + r.width / 2, y = r.y + r.height / 2;
          if (document.elementFromPoint(x, y) === n) return { x, y };
        }
        return null;
      })()
    """ % json.dumps(selector))


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


def click_checkbox_with_label(browser: Browser, label_text: str, root: str = "#main") -> None:
    """Click the `<input type=checkbox>` inside the `label.cb` whose own text is exactly
    `label_text` (the dial's "идеи и рёбра" / "точки-листья" toggles) — same exact-text
    match as `click_button_with_text`, `.cb` labels share no other selector that would
    tell them apart. A real `.click()` on the INPUT itself (not the label) toggles
    `.checked` and fires both "click" and "change" synchronously, exactly what a mouse
    click does and what the view's own `change` listener (`syncParams`) is wired to —
    no separate dispatchEvent needed, unlike `fill()`'s text inputs."""
    js = ("(() => { const labels = [...document.querySelectorAll(%s + ' label.cb')]; "
          "const lab = labels.find((l) => l.textContent.trim() === %s); "
          "const input = lab && lab.querySelector('input'); "
          "if (!input) throw new Error('no label.cb in ' + %s + ' with exact text ' + %s); "
          "input.click(); })()"
          % (json.dumps(root), json.dumps(label_text), json.dumps(root), json.dumps(label_text)))
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


def wait_for_request_count(browser: Browser, pattern: str, target: int, timeout: float = 10) -> int:
    """Polls `browser.count_requests(pattern)` until it reaches (or passes) `target`, instead
    of a fixed `time.sleep` — the access log is written by a separate process (the server),
    so "the click fired" and "the log line landed" are not the same instant. Returns the last
    count seen; raises if `target` is never reached."""
    deadline = time.monotonic() + timeout
    last = browser.count_requests(pattern)
    while time.monotonic() < deadline:
        last = browser.count_requests(pattern)
        if last >= target:
            return last
        time.sleep(0.2)
    raise TimeoutError(f"'{pattern}' only reached {last} requests (want >= {target}) within {timeout}s")


def _install_url_override(browser: Browser, url_substring: str, response_js: str) -> str:
    """Registers an init script (`add_init_script`) that wraps `window.fetch` so any
    request whose URL contains `url_substring` runs `response_js` instead of hitting the
    real network — everything else passes through untouched. `response_js` is raw JS that
    must `return` a Promise (a rejection for a network failure, or `Promise.resolve(new
    Response(...))` for a real status code). Must run before the page's own <script>, which
    is exactly what `add_init_script` (unlike a post-goto `evaluate`) guarantees — needed
    whenever the mock has to be in place for a tab's own MOUNT-time fetch (e.g. the ingest
    tab's `loadStaging()`), not just a later, deliberately-triggered refetch."""
    script = (
        "(() => { const orig = window.fetch; "
        "window.fetch = function(input, init) { "
        "const url = typeof input === 'string' ? input : (input && input.url) || ''; "
        "if (url.includes(%s)) { %s } "
        "return orig.apply(this, arguments); }; })();"
    ) % (json.dumps(url_substring), response_js)

    return browser.add_init_script(script)


def _install_healthz_override(browser: Browser, response_js: str) -> str:
    """`_install_url_override` scoped to "/healthz" — kept as its own name since every call
    site above reads more clearly naming the route it is faking than the generic helper."""
    return _install_url_override(browser, "/healthz", response_js)


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


@test("two_clicks_leave_one_card")
def test_two_clicks_leave_one_card(browser: Browser, base_url: str):
    """Two clicks on DIFFERENT ideas, back to back — the second fired before the first's
    own card has finished rendering — must leave exactly one 'идея …' heading, one
    'Закрыть карточку' button, and one PATCH form, and that surviving form must belong to
    whichever idea the heading (and the address bar) name.

    Measured live on the code before `openIdea()` routed every open through the hash
    (console.html's own `show(idea.id)`/`show(jump.value.trim())`, the exact bypasses
    `address_bar_matches_the_open_idea` targets above): two headings never appeared, but
    two 'Закрыть карточку' buttons and two PATCH forms did, the first still holding idea
    A's own field values under idea B's heading. Reproduced here with `idea_a`'s own GET
    (both `/ideas/{id}` and its `/neighbors`) slowed via a mocked `fetch`, so the second
    click — a real, un-mocked, un-delayed request for `idea_b` — is guaranteed to land
    and render well before `idea_a`'s delayed response arrives.

    Confirmed by mutation: reverting the jump field's 'Открыть' button to call `show(...)`
    directly (its exact pre-fix shape) resurrects the two-card defect this asserts
    against — both closures then share the same `detail` element with no hash-driven
    remount between them. Reverting ONLY the generation guard inside `show()`
    (`if (stale()) return idea;`) while leaving the hash routing intact does NOT: every
    click on a DIFFERENT idea now goes through `openIdea()`, and `render()` fully
    replaces `#main`'s children on each `hashchange`, which orphans idea A's whole closure
    (including its own, un-decremented `generation`) before its delayed response ever
    lands — the two are now independent defenses, not one, and this test's oracle checks
    the SCREEN, not which of the two caught it."""
    with urllib.request.urlopen(f"{base_url}/ideas?limit=2&offset=0", timeout=10) as r:
        ideas = json.load(r)["items"]
    assert len(ideas) >= 2, "stand needs at least two ideas for this test"
    idea_a, idea_b = ideas[0]["id"], ideas[1]["id"]

    browser.goto(f"{base_url}/ui#ideas")
    browser.wait_for("document.querySelector(\"#main input.mono[placeholder='idea_…']\") !== null",
                      timeout=10)
    # Only idea_a's own GET (idea + neighbors, both share this URL prefix) is slowed —
    # idea_b's request, and every other tab's, passes straight through.
    browser.evaluate("""
      (() => {
        window.__e2eOrigFetch = window.fetch;
        window.fetch = function(input, init) {
          const url = typeof input === "string" ? input : (input && input.url) || "";
          if (url.includes(%s)) {
            return new Promise((resolve) =>
              setTimeout(() => resolve(window.__e2eOrigFetch(input, init)), 800));
          }
          return window.__e2eOrigFetch(input, init);
        };
      })()
    """ % json.dumps(f"/ideas/{idea_a}"))
    try:
        browser.fill("#main input.mono[placeholder='idea_…']", idea_a)
        click_button_with_text(browser, "Открыть")   # show(idea_a) — its GET now takes 800ms
        browser.fill("#main input.mono[placeholder='idea_…']", idea_b)
        click_button_with_text(browser, "Открыть")   # fired well before idea_a's card lands
        # idea_b's own request is un-delayed — its card (PATCH form included) finishes
        # comfortably inside idea_a's 800ms.
        browser.wait_for(
            "[...document.querySelectorAll('#main h2')].some((h) => h.textContent === "
            + json.dumps("идея " + idea_b) + ")", timeout=10)
        browser.wait_for(
            "[...document.querySelectorAll('#main h2')].some((h) => "
            "h.textContent.trim() === 'PATCH /ideas/{id}')", timeout=10)
        time.sleep(1.0)   # let idea_a's delayed response land and run its own continuation
    finally:
        browser.evaluate("(() => { if (window.__e2eOrigFetch) { "
                          "window.fetch = window.__e2eOrigFetch; delete window.__e2eOrigFetch; } })()")

    headings = browser.evaluate(
        "[...document.querySelectorAll('#main h2')].filter((h) => "
        "/^идея idea_/.test(h.textContent.trim())).map((h) => h.textContent.trim())")
    close_buttons = browser.evaluate(
        "[...document.querySelectorAll('#main button')].filter((b) => "
        "b.textContent.trim() === " + json.dumps("Закрыть карточку") + ").length")
    patch_forms = browser.evaluate(
        "[...document.querySelectorAll('#main h2')].filter((h) => "
        "h.textContent.trim() === 'PATCH /ideas/{id}').length")

    problems = []
    if len(headings) != 1:
        problems.append(f"expected exactly one 'идея …' heading, found {headings!r}")
    if close_buttons != 1:
        problems.append(f"expected exactly one 'Закрыть карточку' button, found {close_buttons}")
    if patch_forms != 1:
        problems.append(f"expected exactly one PATCH form, found {patch_forms}")
    assert not problems, "; ".join(problems)

    # The one surviving form must belong to whichever idea the heading (and the address
    # bar) actually name — not to the stale first click, which is exactly what "the first
    # form holds idea A's own fields" looked like on the unfixed code.
    shown_id = headings[0].split(" ", 1)[1]
    assert shown_id == idea_b, (
        f"the surviving heading names {shown_id!r}, expected the SECOND click's idea "
        f"{idea_b!r}")
    hash_now = browser.evaluate("location.hash")
    assert hash_now == f"#ideas/{idea_b}", (
        f"heading names {idea_b!r} but location.hash is {hash_now!r}")
    shown_text = read_labeled_number(browser, "text")
    with urllib.request.urlopen(f"{base_url}/ideas/{idea_b}", timeout=10) as r:
        real_b = json.load(r)
    assert shown_text == real_b["text"], (
        f"the one surviving PATCH form's 'text' field is {shown_text!r}, expected idea "
        f"B's own text {real_b['text']!r} — the surviving form belongs to a different idea")


# ==================================================== router: opening an idea (§0.1)

def _assert_idea_link_worked(browser: Browser, label: str, problems: list) -> None:
    """The three things `openIdea()` promises per §2.1, checked against the router's own
    contract (`#<tab>/<arg>`) rather than against some other side effect that could pass by
    accident: the hash becomes `#ideas/idea_…`, the 'Идеи' tab is the one marked active
    (`aria-selected="true"`, console.html:52's own selector), and that exact idea's card is
    actually on screen. Appends a labelled problem to `problems` instead of asserting
    directly — callers drive this from five different call sites and want ALL of their
    failures, not just the first."""
    try:
        browser.wait_for("location.hash.startsWith('#ideas/idea_')", timeout=10)
    except TimeoutError:
        problems.append(f"[{label}] location.hash never became '#ideas/idea_…', stayed "
                         f"{browser.evaluate('location.hash')!r}")
        return
    tab_active = browser.evaluate(
        "[...document.querySelectorAll('nav.tabs button')].some((b) => "
        "b.textContent.trim() === 'Идеи' && b.getAttribute('aria-selected') === 'true')")
    if not tab_active:
        problems.append(f"[{label}] hash is {browser.evaluate('location.hash')!r} but the "
                         f"'Идеи' tab is not the one marked aria-selected=\"true\"")
    idea_id = browser.evaluate("location.hash.slice(1).split('/')[1].split('?')[0]")
    heading = "идея " + (idea_id or "")
    try:
        browser.wait_for("[...document.querySelectorAll('#main h2')].some((h) => h.textContent === "
                          + json.dumps(heading) + ")", timeout=10)
    except TimeoutError:
        problems.append(f"[{label}] hash points at {idea_id!r} but no <h2>{heading!r}</h2> "
                         f"ever rendered under #main")


@test("click_idea_node_opens_it_on_first_load")
def test_click_idea_node_opens_it_on_first_load(browser: Browser, base_url: str):
    """§0.1, the headline defect: a fresh /ui load that has NEVER opened 'Идеи' must still
    open an idea on the FIRST click of a dial node. Before the fix, `openIdea` was declared
    a stub (`let openIdea = () => {}`, console.html:1129 as of 90cdbe3) and only assigned a
    real body from INSIDE the 'Идеи' view's own mount — every one of the five call sites
    was dead until an operator happened to visit 'Идеи' by hand first, which is exactly
    what this test refuses to do before clicking. Confirmed by mutation (see this task's
    write-up): patching `window.openIdea` back to that literal stub at runtime, right
    before the click, leaves `location.hash` at '#dial' and this test's own
    `wait_for(location.hash.startsWith(...))` times out — it does not pass quietly."""
    hypothesis = "e2e first-load idea click probe"
    browser.goto(f"{base_url}/ui#dial")   # never visited "Идеи" in this document
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    browser.fill("#main textarea", hypothesis)
    click_button_with_text(browser, "Разложить")
    browser.wait_for("document.querySelectorAll('.graphwrap svg circle.ideanode').length > 0", timeout=20)

    pt = find_hittable_point(browser, ".graphwrap svg circle.ideanode")
    assert pt, "no dial idea node is reachable by a real click — every one is occluded"
    browser.click_point(pt["x"], pt["y"])
    problems = []
    _assert_idea_link_worked(browser, "dial node, first load", problems)
    assert not problems, "; ".join(problems)


@test("every_idea_link_opens_the_idea")
def test_every_idea_link_opens_the_idea(browser: Browser, base_url: str):
    """§0.1's full list of five: the dial's own node (already isolated above), the dial's
    OWN hit table, the 'Индекс' tab's hit table, the 'Тезисы' tab's idea column, and the
    'Граф' tab's «Открыть в „Идеях“» button — all five go through the same `openIdea()`.
    Every case starts from its own fresh `goto()` (the `_t=` param in `Browser.goto` forces
    a real document load every single time, per its own docstring) that never visits
    'Идеи' first — the original bug's whole shape was "works the second tab you try, once
    someone else warmed up 'Идеи' in this same document", so warming it up here would hide
    exactly what this test exists to catch. Collects every failing site instead of stopping
    at the first, same reasoning as `wrong_shape_200_is_an_error_not_a_spinner` above."""
    with urllib.request.urlopen(f"{base_url}/ideas?limit=1&offset=0", timeout=10) as r:
        seed_idea_id = json.load(r)["items"][0]["id"]

    problems = []

    # (a) dial node
    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    browser.fill("#main textarea", "e2e link probe: dial node")
    click_button_with_text(browser, "Разложить")
    browser.wait_for("document.querySelectorAll('.graphwrap svg circle.ideanode').length > 0", timeout=20)
    pt = find_hittable_point(browser, ".graphwrap svg circle.ideanode")
    if pt:
        browser.click_point(pt["x"], pt["y"])
        _assert_idea_link_worked(browser, "dial node", problems)
    else:
        problems.append("[dial node] no idea node was reachable by a real click at all")

    # (b) dial hit table (console.html's renderHits — separate from the circle above)
    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    browser.fill("#main textarea", "e2e link probe: dial hit table")
    click_button_with_text(browser, "Разложить")
    browser.wait_for("document.querySelector('#main table.tbl tbody tr td.mono a') !== null", timeout=20)
    browser.click("#main table.tbl tbody tr:first-child td.mono a")
    _assert_idea_link_worked(browser, "dial hit table", problems)

    # (c) "Индекс" tab hit table
    browser.goto(f"{base_url}/ui#search")
    browser.wait_for("document.querySelector('#main input.grow') !== null", timeout=10)
    browser.fill("#main input.grow", "e2e link probe: index hit table")
    click_button_with_text(browser, "Искать")
    browser.wait_for("document.querySelector('#main table.tbl tbody tr td.mono a') !== null", timeout=15)
    browser.click("#main table.tbl tbody tr:first-child td.mono a")
    _assert_idea_link_worked(browser, "Индекс hit table", problems)

    # (d) "Тезисы" tab idea column (auto-loads on mount, no button click needed to see rows)
    browser.goto(f"{base_url}/ui#theses")
    browser.wait_for("document.querySelector('#main table.tbl tbody tr td.mono a') !== null", timeout=15)
    browser.click("#main table.tbl tbody tr:first-child td.mono a")
    _assert_idea_link_worked(browser, "Тезисы idea column", problems)

    # (e) "Граф" tab "Открыть в «Идеях»" — seeded with a real id so this draws an ego graph
    # (2 requests) instead of the whole-lake sweep an empty seed would cost (N+1 requests).
    browser.goto(f"{base_url}/ui#graph")
    browser.wait_for("document.querySelector('#main input.grow') !== null", timeout=10)
    browser.fill("#main input.grow", seed_idea_id)
    click_button_with_text(browser, "Нарисовать")
    browser.wait_for("document.querySelector('.graphwrap svg circle.node') !== null", timeout=20)
    node_pt = find_hittable_point(browser, ".graphwrap svg circle.node")
    assert node_pt, "no graph node is reachable by a real click at all"
    browser.click_point(node_pt["x"], node_pt["y"])
    browser.wait_for(
        "[...document.querySelectorAll('#main button')].some((b) => b.textContent.trim() === "
        + json.dumps("Открыть в «Идеях»") + ")", timeout=10)
    click_button_with_text(browser, "Открыть в «Идеях»")
    _assert_idea_link_worked(browser, "Граф «Открыть в „Идеях“»", problems)

    assert not problems, "idea links that did not open the idea: " + "; ".join(problems)


@test("idea_link_is_a_shareable_url")
def test_idea_link_is_a_shareable_url(browser: Browser, base_url: str):
    """§2.1's own payoff, stated in the spec directly: a link built by `openIdea()` must
    work pasted into a brand new document, no clicks. Opens `/ui#ideas/<real id>` as a
    fresh `goto()` (never having been an in-page hashchange from anywhere) and checks the
    idea is on screen immediately — no click, no prior visit to any other tab."""
    with urllib.request.urlopen(f"{base_url}/ideas?limit=1&offset=0", timeout=10) as r:
        idea_id = json.load(r)["items"][0]["id"]

    browser.goto(f"{base_url}/ui#ideas/{idea_id}")
    heading = "идея " + idea_id
    browser.wait_for("[...document.querySelectorAll('#main h2')].some((h) => h.textContent === "
                      + json.dumps(heading) + ")", timeout=10)
    tab_active = browser.evaluate(
        "[...document.querySelectorAll('nav.tabs button')].some((b) => "
        "b.textContent.trim() === 'Идеи' && b.getAttribute('aria-selected') === 'true')")
    assert tab_active, "opening #ideas/<id> directly did not land on the 'Идеи' tab"


def _find_idea_with_neighbor(base_url: str, scan_limit: int = 50):
    """First idea (scanning `/ideas`) with at least one real edge — needed to click a
    live edge-neighbor link rather than assume one exists."""
    with urllib.request.urlopen(f"{base_url}/ideas?limit={scan_limit}&offset=0", timeout=10) as r:
        ideas = json.load(r)["items"]
    for idea in ideas:
        with urllib.request.urlopen(
                f"{base_url}/ideas/{idea['id']}/neighbors?hops=1", timeout=10) as r:
            edges = json.load(r)
        if edges:
            return idea["id"]
    raise AssertionError(f"no idea in the first {scan_limit} has any neighbor — "
                          f"address_bar_matches_the_open_idea needs one")


@test("address_bar_matches_the_open_idea")
def test_address_bar_matches_the_open_idea(browser: Browser, base_url: str):
    """§1.2: 'ссылка на состояние — это URL', checked against every real way an operator
    opens or closes a card inside the 'Идеи' tab itself — not just the five call sites
    `every_idea_link_opens_the_idea` already covers, which all go through `openIdea()`
    from OUTSIDE this tab. Three entry points that live INSIDE 'Идеи' used to call
    `show(id)` directly (the table row's own 'открыть' button, the 'открыть по id'
    field, and the edge-neighbor link rendered in an open card) — bypassing the router
    entirely, so the card on screen and the address bar could each name a different
    idea. 'Закрыть карточку' has the opposite fault: clearing the DOM without clearing
    the hash means an F5 right after closing reopens exactly what was just closed, and
    a second click on that same idea's own link goes nowhere (setting `location.hash`
    to the value it already holds fires no `hashchange`). Collects every broken path
    instead of stopping at the first, same reasoning as `every_idea_link_opens_the_idea`."""
    idea_a = _find_idea_with_neighbor(base_url)
    problems = []

    # (a) the table row's own "открыть" button.
    browser.goto(f"{base_url}/ui#ideas")
    browser.wait_for("document.querySelector('#main table.tbl tbody tr') !== null", timeout=10)
    row_id = browser.evaluate(
        "document.querySelector('#main table.tbl tbody tr td.mono').textContent.trim()")
    click_button_with_text(browser, "открыть")
    browser.wait_for("[...document.querySelectorAll('#main h2')].some((h) => h.textContent === "
                      + json.dumps("идея " + row_id) + ")", timeout=10)
    hash_now = browser.evaluate("location.hash")
    if hash_now != f"#ideas/{row_id}":
        problems.append(f"[table row 'открыть'] card shows {row_id!r} but location.hash "
                         f"is {hash_now!r}, expected '#ideas/{row_id}'")

    # (b) the "открыть по id" field.
    browser.goto(f"{base_url}/ui#ideas")
    browser.wait_for("document.querySelector(\"#main input.mono[placeholder='idea_…']\") !== null",
                      timeout=10)
    browser.fill("#main input.mono[placeholder='idea_…']", idea_a)
    click_button_with_text(browser, "Открыть")
    browser.wait_for("[...document.querySelectorAll('#main h2')].some((h) => h.textContent === "
                      + json.dumps("идея " + idea_a) + ")", timeout=10)
    hash_now = browser.evaluate("location.hash")
    if hash_now != f"#ideas/{idea_a}":
        problems.append(f"['открыть по id'] card shows {idea_a!r} but location.hash "
                         f"is {hash_now!r}, expected '#ideas/{idea_a}'")

    # (c) the edge-neighbor link rendered inside an open card. Opens idea_a via the one
    # already-proven-good path (a direct #ideas/<id> goto, `idea_link_is_a_shareable_url`)
    # so this case isolates the neighbor link's own onclick alone.
    browser.goto(f"{base_url}/ui#ideas/{idea_a}")
    browser.wait_for("[...document.querySelectorAll('#main h2')].some((h) => h.textContent === "
                      + json.dumps("идея " + idea_a) + ")", timeout=10)
    browser.wait_for("document.querySelector('#main table.tbl tbody tr td.mono a') !== null",
                      timeout=10)
    neighbor_id = browser.evaluate(
        "document.querySelector('#main table.tbl tbody tr td.mono a').textContent.trim()")
    # A real DOM `.click()`, not `Browser.click()`'s CDP mouse-coordinate dispatch — this
    # single-line anchor sits right at a `scrollIntoView` boundary, and coordinate clicks
    # on it landed on the right element (verified: `elementFromPoint` at the computed
    # centre returned this exact `<a>` every time) yet still missed the handler on a
    # majority of runs. `.click()` fires the same real "click" event the page's own
    # `addEventListener` wiring reacts to (see `click_button_with_text`'s docstring above),
    # without CDP's synthetic-mouse hit-testing in the way.
    browser.evaluate("document.querySelector('#main table.tbl tbody tr td.mono a').click()")
    try:
        browser.wait_for("[...document.querySelectorAll('#main h2')].some((h) => h.textContent === "
                          + json.dumps("идея " + neighbor_id) + ")", timeout=10)
    except TimeoutError:
        problems.append(f"[edge-neighbor link] clicking the link to {neighbor_id!r} never "
                         f"opened its card — heading stayed on {idea_a!r}, "
                         f"location.hash is {browser.evaluate('location.hash')!r}")
    else:
        hash_now = browser.evaluate("location.hash")
        if hash_now != f"#ideas/{neighbor_id}":
            problems.append(f"[edge-neighbor link] card shows {neighbor_id!r} but "
                             f"location.hash is {hash_now!r}, expected '#ideas/{neighbor_id}'")

    # (d) "Закрыть карточку" must take the id back OUT of the hash — an F5 right after
    # closing must not reopen the idea that was just closed.
    browser.goto(f"{base_url}/ui#ideas/{idea_a}")
    browser.wait_for("[...document.querySelectorAll('#main button')].some((b) => "
                      "b.textContent.trim() === " + json.dumps("Закрыть карточку") + ")", timeout=10)
    click_button_with_text(browser, "Закрыть карточку")
    hash_after_close = browser.evaluate("location.hash")
    if re.search(r"/idea_", hash_after_close):
        problems.append(f"['Закрыть карточку'] location.hash is still {hash_after_close!r} "
                         f"after closing — an F5 now would reopen the just-closed idea")

    # (e) reopening the SAME idea after closing must not be a dead click. If (d) left the
    # id in the hash, `openIdea(idea_a)` sets `location.hash` to the value it already
    # holds — no `hashchange` fires, and every one of the five real click sites
    # `every_idea_link_opens_the_idea` covers does exactly this call, so calling it
    # directly is a faithful stand-in that does not depend on which of the five a
    # fragile dial/search result happens to surface.
    browser.evaluate(f"openIdea({json.dumps(idea_a)})")
    try:
        browser.wait_for("[...document.querySelectorAll('#main h2')].some((h) => h.textContent === "
                          + json.dumps("идея " + idea_a) + ")", timeout=3)
    except TimeoutError:
        problems.append(f"[reopen after close] clicking idea {idea_a}'s own link again "
                         f"after closing it did nothing — location.hash is "
                         f"{browser.evaluate('location.hash')!r}, the card never came back")

    assert not problems, "; ".join(problems)


@test("unknown_tab_is_not_a_white_screen")
def test_unknown_tab_is_not_a_white_screen(browser: Browser, base_url: str):
    """render()'s fallback for a hand-edited or stale hash (console.html ~2222-2228): an
    unknown tab id must not read as a blank page indistinguishable from a crash. Checks
    both halves — the FIRST tab's own content still renders under #main, AND a
    '.status.warn' names the actual unknown tab in words, not just a silent fallback that
    looks identical to having typed a real one."""
    browser.goto(f"{base_url}/ui#nosuchtab")
    # render() draws a plain ".panel" spinner while AUTH === "probing" (console.html
    # ~2233-2237), which also satisfies "#main .panel" — so that alone can't be the gate,
    # it reads the DOM before the unknown-tab warning has ever been drawn. Wait out the
    # probe first (same condition the four boot_probe_* tests above use), then wait for
    # the warning itself, as a real wait and not a one-shot query — a warning that never
    # shows up must time out here, not read as "not yet rendered".
    browser.wait_for("typeof AUTH !== 'undefined' && AUTH !== 'probing'", timeout=10)
    browser.wait_for("document.querySelector('#main .status.warn') !== null", timeout=10)

    warning = browser.evaluate(
        "(() => { const b = document.querySelector('#main .status.warn'); "
        "return b ? b.textContent : null; })()")
    assert warning is not None, "no '.status.warn' explanation shown for an unknown tab in the hash"
    assert "nosuchtab" in warning, f"the warning does not name the unknown tab by its actual text: {warning!r}"

    # Not just a warning banner over an empty #main: the fallback tab's OWN content (its
    # real controls, not a second copy of the warning) must actually be usable.
    has_controls = browser.evaluate(
        "document.querySelectorAll('#main input, #main textarea, #main button').length > 0")
    assert has_controls, "the fallback tab shows the warning but no usable controls under #main"


@test("selftest_block_passes")
def test_selftest_block_passes(browser: Browser, base_url: str):
    """§3's page-level self-check: `?selftest=1` (on the DOCUMENT, never the hash — checked
    against a `#dial` hash on purpose, to prove the gate reads `location.search`, not the
    route under test) must print exactly 'SELFTEST OK' to the browser console and raise no
    error. Read from the REAL browser console (`Browser.console_messages`, backed by CDP's
    `Runtime.consoleAPICalled` / `Runtime.exceptionThrown`), not by re-running the
    assertions in Python — that would only prove Python's copy of the logic is right, not
    that the page's own selftest block exists and passed. Must fail loudly if the block is
    missing entirely: an empty console log satisfies neither the error-count check (0 is
    fine) nor the 'SELFTEST OK' check (never found) — there is no way for "no selftest
    shipped" to read as green here, unlike a bare `assert not errors` would."""
    browser.goto(f"{base_url}/ui?selftest=1#dial")
    # The selftest IIFE runs synchronously near the end of the page's own inline <script>,
    # well before `readyState=="complete"` (which `goto()` already waited for) — but the
    # CDP event carrying it across the websocket is still one more async hop behind that,
    # so this polls Python-side instead of assuming it has already landed.
    deadline = time.monotonic() + 5
    messages = []
    while time.monotonic() < deadline:
        messages = browser.console_messages()
        if any("SELFTEST" in m["text"] for m in messages):
            break
        time.sleep(0.1)

    errors = [m for m in messages if m["type"] in ("error", "exception")]
    assert not errors, f"selftest run logged {len(errors)} error(s)/exception(s): {errors}"
    ok = any(m["text"].strip() == "SELFTEST OK" for m in messages)
    assert ok, (
        f"'SELFTEST OK' never appeared in the browser console — either the selftest block "
        f"is missing, or it threw before reaching its last line. Console messages seen: "
        f"{messages}")


# ============================================================ five defects, #15-#19
#
# Each test below feeds a 200 response with an UNRELATED body ({"surprise": ...}) through
# a mocked `window.fetch`, method-scoped so the mock only ever catches the ONE write or
# read this test cares about — never the tab's own unrelated baseline load. Same
# "captive portal" fault as `wrong_shape_200_is_an_error_not_a_spinner` above, applied to
# five call sites that test does not reach: the Retrieve tab's own POST, the dial's
# "Перечитать граф" (which must also leave the last-good graph undisturbed, not just
# show an error), POST /sources, the ingest queue's quiet auto-refresh (fired bare from
# setInterval, nothing above it to catch a stray throw), and PATCH /ideas/{id} (which
# must not read a malformed response as a small, honest write).

@test("retrieve_wrong_shape_200_is_an_error")
def test_retrieve_wrong_shape_200_is_an_error(browser: Browser, base_url: str):
    """#15: POST /retrieve now shape-checks its own response ({ideas, cost}) before handing
    it to answerBlock() — a 200 with an unrelated body must land on '.status.err', not a
    spinner stuck forever after run() already thinks the call succeeded. The Retrieve tab
    is NOT one of `wrong_shape_200_is_an_error_not_a_spinner`'s `configs` above (that list
    only drives ideas/theses/sources/search), so this is the only place it gets covered."""
    browser.goto(f"{base_url}/ui#retrieve")
    browser.wait_for("document.querySelector('#main input.grow') !== null", timeout=10)
    browser.fill("#main input.grow", "e2e retrieve shape probe")

    _install_method_shape_override(browser, "/retrieve", "POST")
    click_button_with_text(browser, "Спросить озеро")
    browser.wait_for("document.querySelector('#main .status.err') !== null", timeout=10)

    text = status_text_sans_code(browser)
    assert "cost" in text.lower(), (
        f"expected the /retrieve shape-check's own message ('не {{ideas, cost}}') in the "
        f"error status, got {text!r}")
    spinner = browser.evaluate("document.querySelector('#main .spinner') !== null")
    assert not spinner, "a spinner is still in the DOM after the wrong-shape 200 settled into an error"


@test("graph_reread_failure_keeps_prior_graph")
def test_graph_reread_failure_keeps_prior_graph(browser: Browser, base_url: str):
    """#16: 'Перечитать граф' (dial tab) must not redraw over a FAILED reread — before the
    fix, `fire()` ran unconditionally right after `loadGraph(true)`, so a captive-portal 200
    for /edges left the usual run()-catch error on screen for an instant and then plunged
    into fire() anyway, drawing over it. Proven directly: draw the dial for real once,
    count its circles, force the NEXT /edges call to answer 200 with an unrelated body,
    click 'Перечитать граф', and check both that the error names the graph as unchanged
    ('граф остался прежним') and that the SVG genuinely did not move — same circle count
    as before the click, not zero and not redrawn from a half-read graph."""
    hypothesis = "e2e graph reread probe"
    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    browser.fill("#main textarea", hypothesis)
    click_button_with_text(browser, "Разложить")
    browser.wait_for("document.querySelector('.graphwrap svg') !== null", timeout=20)
    nodes_before = browser.evaluate("document.querySelectorAll('.graphwrap svg circle').length")
    assert nodes_before > 0, (
        "dial drew no circles at all — this test needs a real drawing to compare against")

    _install_method_shape_override(browser, "/edges", "GET")
    click_button_with_text(browser, "Перечитать граф")
    browser.wait_for("document.querySelector('#main .status.err') !== null", timeout=10)

    # Hard invariant FIRST: whatever the error says, fire() must not have run at all — a
    # message this test would otherwise never get to check if the harder, silent-corruption
    # failure fired first and obscured it.
    nodes_after = browser.evaluate("document.querySelectorAll('.graphwrap svg circle').length")
    assert nodes_after == nodes_before, (
        f"the dial redrew after a FAILED reread: {nodes_before} circles before the click, "
        f"{nodes_after} after — fire() must not run when the reread's own run() caught "
        f"an error")

    # Softer UX requirement second: the code's own catch only appends " — граф остался
    # прежним" when `err instanceof ApiError` (console.html ~1118) — a malformed 200 for
    # /edges throws a plain TypeError from `edges.push(...page.items)` instead (page.items
    # is undefined), which is NOT an ApiError, so it skips that branch and reaches the
    # operator as a bare "ответ не той формы · page.items is not iterable" with no
    # reassurance that the graph itself is untouched. The state IS safe (checked above) —
    # this is a real, narrower gap: the "остался прежним" guarantee the fix's own comment
    # promises does not yet cover this fault shape, only the ApiError one.
    text = status_text_sans_code(browser)
    assert "граф остался прежним" in text, (
        f"expected 'граф остался прежним' in the reread's error status, got {text!r} — "
        f"the state IS safe (circle count unchanged, checked above), but the catch in "
        f"console.html's 'Перечитать граф' handler only adds this reassurance for "
        f"`err instanceof ApiError`, not for a plain render-time TypeError from a "
        f"malformed 200 (this test's fault)")


@test("sources_post_wrong_shape_200_is_an_error")
def test_sources_post_wrong_shape_200_is_an_error(browser: Browser, base_url: str):
    """#17: POST /sources now shape-checks the row it gets back (needs a string `.id`)
    before interpolating it into the confirmation line — a 200 of the wrong shape used to
    print a calm green "записан undefined" instead of surfacing as an error. Method-scoped
    to POST: /sources is ALSO the tab's own GET on mount, and mocking that too would break
    the unrelated listing read this test does not care about."""
    browser.goto(f"{base_url}/ui#sources")
    browser.wait_for("document.querySelector('#main .pager') !== null", timeout=10)   # real GET landed first

    browser.fill("#main details.panel input.grow[placeholder='https://…']",
                 "https://example.com/e2e-post-sources-shape-probe")
    browser.evaluate(
        "(() => { const t = [...document.querySelectorAll('#main details.panel input.grow')][1]; "
        "t.value = 'e2e post sources shape probe'; "
        "t.dispatchEvent(new Event('input', {bubbles: true})); "
        "t.dispatchEvent(new Event('change', {bubbles: true})); })()")

    _install_method_shape_override(browser, "/sources", "POST")
    click_button_with_text(browser, "Записать")
    browser.wait_for("document.querySelector('#main details.panel .status.err') !== null", timeout=10)

    text = browser.evaluate(
        "(() => { const box = document.querySelector('#main details.panel .status'); "
        "const clone = box.cloneNode(true); const code = clone.querySelector('.code'); "
        "if (code) code.remove(); return clone.textContent; })()")
    assert "без id" in text, f"expected 'ответ /sources без id' in the error status, got {text!r}"
    assert "undefined" not in text, f"the old calm lie ('записан undefined') is still showing: {text!r}"


@test("ingest_queue_autorefresh_wrong_shape_shows_error")
def test_ingest_queue_autorefresh_wrong_shape_shows_error(browser: Browser, base_url: str):
    """#18: the quiet auto-refresh path (`loadJobs(true)`, fired bare from `setInterval`
    with nothing above it to catch a stray throw) now shape-checks /ingest/jobs itself
    instead of letting `jobs.filter` throw as an unhandled rejection — which used to leave
    the 'автообновление' checkbox checked and the LAST GOOD row on screen forever while
    every tick silently failed underneath it. That is a STALE status that never changes,
    not a stuck spinner, and just as much a lie — so this waits for the status to actually
    flip to err rather than reading it once right after the click."""
    panel = panel_js("GET /ingest/jobs")
    browser.goto(f"{base_url}/ui#ingest")
    browser.wait_for(f"(() => {{ const p = {panel}; const b = p && p.querySelector('.status'); "
                      "return b && !b.querySelector('.spinner'); })()", timeout=10)
    before_text = browser.evaluate(f"(() => {{ const p = {panel}; return p.querySelector('.status').textContent; }})()")

    _install_method_shape_override(browser, "/ingest/jobs", "GET")
    browser.click("#main input[type=checkbox]")   # the tab's only checkbox: "автообновление 4 с"

    browser.wait_for(f"(() => {{ const p = {panel}; const b = p.querySelector('.status'); "
                      "return b && b.className.split(' ').includes('err'); })()", timeout=10)

    text = browser.evaluate(
        f"(() => {{ const p = {panel}; const clone = p.querySelector('.status').cloneNode(true); "
        "const code = clone.querySelector('.code'); if (code) code.remove(); "
        "return clone.textContent; })()")
    assert "не массив" in text, (
        f"expected 'ответ /ingest/jobs — не массив' in the auto-refresh error status, got {text!r}")
    assert text != before_text, "status text did not actually change from the pre-refresh reading"


@test("patch_idea_wrong_shape_response_is_an_error_not_a_false_write")
def test_patch_idea_wrong_shape_response_is_an_error(browser: Browser, base_url: str):
    """#19: PATCH /ideas/{id} now checks that its own response actually IS the patched
    idea (`res.id === ideaId`, `text` a string, `theses` an array) before it ever prints
    "записано" — a 200 of the wrong shape used to read `updated.updated_at` as undefined,
    fall back to the page's own honest '«»' marker for "server didn't stamp a time", and
    confirm a write that never actually landed as that idea. Checks both halves: the error
    status appears in the PATCH form's OWN status host (`patchStatus`, scoped via
    '.status.err' since the idea-read status above it is a second, unrelated '.status' in
    the same DOM), and the "записано" confirmation (`writeStatus`, a THIRD host — see
    `write_confirmation_survives_the_reread` above) never gets set at all."""
    with urllib.request.urlopen(f"{base_url}/ideas?limit=1&offset=0", timeout=10) as r:
        idea_id = json.load(r)["items"][0]["id"]

    browser.goto(f"{base_url}/ui#ideas")
    browser.wait_for("document.querySelector(\"#main input.mono[placeholder='idea_…']\") !== null",
                      timeout=10)
    browser.fill("#main input.mono[placeholder='idea_…']", idea_id)
    click_button_with_text(browser, "Открыть")
    browser.wait_for(
        "[...document.querySelectorAll('#main h2')].some((h) => h.textContent.includes('PATCH /ideas'))",
        timeout=10)

    set_labeled_input(browser, "text", "e2e mutated text " + idea_id)
    _install_method_shape_override(browser, "/ideas/", "PATCH")
    click_button_with_text(browser, "Записать изменения")
    browser.wait_for("document.querySelector('#main .status.err') !== null", timeout=10)

    text = status_text_sans_code(browser, "#main .status.err")
    assert "не идея" in text, (
        f"expected 'ответ PATCH /ideas/{{id}} — не идея' in the error status, got {text!r}")

    wrote = browser.evaluate(
        "[...document.querySelectorAll('#main .status')].some((b) => b.textContent.includes('записано:'))")
    assert not wrote, "a write confirmation ('записано:') is showing after a wrong-shape PATCH response"


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


def _install_method_shape_override(browser: Browser, url_substring: str, method: str) -> None:
    """Like `_install_shape_override`, but only intercepts requests of the given HTTP
    `method` — several routes below are read AND written through the same path
    (`GET /sources` on tab load vs. `POST /sources` on submit, `GET /ideas/{id}` on open
    vs. `PATCH /ideas/{id}` on save), and mocking the write half must not also break the
    unrelated read half's own, separately-tested shape check. Same non-restoration
    reasoning as `_install_shape_override`."""
    browser.evaluate("""
      (() => {
        const orig = window.fetch;
        window.fetch = function(input, init) {
          const url = typeof input === "string" ? input : (input && input.url) || "";
          const m = ((init && init.method) || "GET").toUpperCase();
          if (m === %s && url.includes(%s)) {
            return Promise.resolve(new Response(JSON.stringify({surprise: "not a page"}),
              {status: 200, headers: {"Content-Type": "application/json"}}));
          }
          return orig(input, init);
        };
      })()
    """ % (json.dumps(method.upper()), json.dumps(url_substring)))


def panel_js(heading: str, root: str = "#main") -> str:
    """JS expression (a string to splice into a larger `evaluate`/`wait_for` call) that
    finds whichever `.panel` under `root` has an `<h2>` matching `heading` exactly, or
    `null`. The ingest tab keeps six panels each with their own status host (POST /fetch,
    Фазы, Обслуживание, GET /ingest/jobs, staging, pending-link) — a bare `#main .status`
    query silently reads the wrong one the moment more than one has ever fired, which on
    this tab is the normal case (every panel below the first fires its own request on
    mount)."""
    return ("(() => { const panels = [...document.querySelectorAll(%s + ' .panel')]; "
            "return panels.find((p) => { const h = p.querySelector('h2'); "
            "return h && h.textContent.trim() === %s; }) || null; })()"
            % (json.dumps(root), json.dumps(heading)))


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


# ==================================================== §0.2/§2.2: state survives a switch

@test("state_survives_a_tab_round_trip")
def test_state_survives_a_tab_round_trip(browser: Browser, base_url: str):
    """§0.2/§2.2, the main new guarantee: a tab switch away and back must restore a view
    from `VIEW_STATE` (memory), not from a second round trip to the server. Checked on all
    three shapes of state the spec names — dial's hypothesis+k+hits+SVG, retrieve's
    query+answer, and the SAME pager-offset string that sits in all three list tabs
    («Идеи», «Тезисы», «Источники») — because each lives in its own view closure (§0.2's
    own list) and a fix (or a regression) proven on one says nothing about the others.
    §8.3's M13 found exactly that hole: only «Идеи» used to be checked here, so a broken
    offset on «Тезисы» or «Источники» stayed green. Every case checks BOTH halves the task
    asks for: the SCREEN (unchanged) and the server's own access log (no repeat request) —
    restored from memory, not re-fetched.

    `away` (`#raw`) is a neutral tab that fires no request of its own on mount, so a
    request count taken either side of the round trip attributes any growth to the
    view being tested, not to whatever tab sits in between."""
    problems = []
    away_wait = "document.querySelector('#main input.mono.grow') !== null"   # raw tab's own input

    # --- dial: hypothesis text, k, hit table, SVG ---
    hypothesis = f"e2e_state_round_trip_dial_{int(time.time() * 1000)}"
    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    browser.fill("#main textarea", hypothesis)
    set_labeled_input(browser, "k", "7")
    click_button_with_text(browser, "Разложить")
    browser.wait_for("document.querySelectorAll('.graphwrap svg circle.ideanode').length > 0", timeout=20)

    rows_before = browser.evaluate("document.querySelectorAll('#main table.tbl tbody tr').length")
    status_before = status_text_sans_code(browser)
    dial_before = browser.count_requests("GET /dial?")

    browser.evaluate("location.hash = '#raw'")
    browser.wait_for(away_wait, timeout=10)
    browser.evaluate("location.hash = '#dial'")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)

    text_now = browser.evaluate("document.querySelector('#main textarea').value")
    k_now = read_labeled_number(browser, "k")
    rows_now = browser.evaluate("document.querySelectorAll('#main table.tbl tbody tr').length")
    svg_now = browser.evaluate("document.querySelector('.graphwrap svg') !== null")
    status_now = status_text_sans_code(browser)
    dial_after = browser.count_requests("GET /dial?")

    if text_now != hypothesis:
        problems.append(f"[dial] hypothesis text lost across a tab round trip: {text_now!r}")
    if k_now != "7":
        problems.append(f"[dial] k reverted to {k_now!r} across a tab round trip, expected '7'")
    if rows_now != rows_before:
        problems.append(f"[dial] hit table rows changed across a tab round trip: {rows_before} -> {rows_now}")
    if not svg_now:
        problems.append("[dial] SVG is gone after a tab round trip")
    if status_now != status_before:
        problems.append(f"[dial] status text changed across a tab round trip: {status_before!r} -> {status_now!r}")
    if dial_after != dial_before:
        problems.append(f"[dial] GET /dial fired again on return ({dial_before} -> {dial_after}) "
                         f"— restored by a repeat request, not from memory")

    # Дыра 4: this round trip goes through a BARE hash ('#dial', no query string) on
    # purpose, which is exactly what proves the SCREEN is restored from VIEW_STATE rather
    # than a repeat request — but the same bare hash is also where the address itself used
    # to degenerate (defect 3): the screen came back, the link in the bar did not. Checked
    # here, not just on screen: a reload right after this round trip must reopen the SAME
    # hypothesis, which only holds if `location.href` carries `q=...`, not a bare '#dial'.
    href_now = browser.evaluate("location.href")
    if "?" not in href_now.split("#", 1)[-1]:
        problems.append(f"[dial] the address is still a bare hash after the round trip "
                         f"(no query string at all): {href_now!r} — a reload here would lose "
                         f"the hypothesis even though the screen still shows it")
    elif f"q={hypothesis}" not in href_now:
        problems.append(f"[dial] the address after the round trip does not carry the "
                         f"hypothesis {hypothesis!r}: {href_now!r}")

    # --- retrieve: query, rendered answer ---
    query = f"e2e_state_round_trip_retrieve_{int(time.time() * 1000)}"
    browser.goto(f"{base_url}/ui#retrieve")
    browser.wait_for("document.querySelector('#main input.grow') !== null", timeout=10)
    browser.fill("#main input.grow", query)
    click_button_with_text(browser, "Спросить озеро")
    browser.wait_for("document.querySelector('#main .status.ok') !== null", timeout=20)

    # `out` (the answer container `host.replaceChildren(panel, out)` builds) is #main's
    # second top-level child — it carries no class/id of its own to select on directly.
    out_sel = "document.querySelectorAll('#main > div')[1]"
    answer_before = browser.evaluate(f"{out_sel}.innerHTML")
    query_before = browser.evaluate("document.querySelector('#main input.grow').value")
    retrieve_before = browser.count_requests("POST /retrieve")

    browser.evaluate("location.hash = '#raw'")
    browser.wait_for(away_wait, timeout=10)
    browser.evaluate("location.hash = '#retrieve'")
    browser.wait_for("document.querySelector('#main input.grow') !== null", timeout=10)

    query_now = browser.evaluate("document.querySelector('#main input.grow').value")
    answer_now = browser.evaluate(f"{out_sel} ? {out_sel}.innerHTML : null")
    retrieve_after = browser.count_requests("POST /retrieve")

    if query_now != query_before:
        problems.append(f"[retrieve] query text lost across a tab round trip: {query_now!r}")
    if answer_now != answer_before:
        problems.append("[retrieve] the rendered answer changed across a tab round trip")
    if retrieve_after != retrieve_before:
        problems.append(f"[retrieve] POST /retrieve fired again on return "
                         f"({retrieve_before} -> {retrieve_after}) — restored by a repeat "
                         f"request, not from memory")

    # Дыра 4, same reasoning as the dial section above: the round trip just taken is
    # through a bare '#retrieve', not '#retrieve?q=...'.
    href_now = browser.evaluate("location.href")
    if "?" not in href_now.split("#", 1)[-1]:
        problems.append(f"[retrieve] the address is still a bare hash after the round trip: "
                         f"{href_now!r}")
    elif f"q={query}" not in href_now:
        problems.append(f"[retrieve] the address after the round trip does not carry the "
                         f"query {query!r}: {href_now!r}")

    # --- ideas / theses / sources: pager offset, on ALL THREE list tabs (§8.3's M13) ---
    # The same "показано A–B из C" string and the same pager() component sit in «Идеи»,
    # «Тезисы» and «Источники» alike, each behind its own view closure with its own
    # `state.offset` — a fix (or a break) proven on one tab says nothing about the other
    # two, which is exactly how a broken offset on «Тезисы»/«Источники» stayed green
    # before this loop existed here.
    for tab, request_prefix in [("ideas", "GET /ideas?"), ("theses", "GET /theses?"),
                                 ("sources", "GET /sources?")]:
        browser.goto(f"{base_url}/ui#{tab}")
        browser.wait_for("document.querySelector('#main .pager') !== null", timeout=10)
        before_text = browser.evaluate("document.querySelector('#main .pager').textContent")
        from0, to0, total0 = parse_pager(before_text)
        if from0 != 1 or total0 <= to0:
            problems.append(f"[{tab}] stand has too little data to page forward ({before_text!r}) "
                             f"— this part of the test needs more than one page")
            continue
        page_size = to0 - from0 + 1
        click_pager_forward(browser, before_text)
        moved_text = browser.evaluate("document.querySelector('#main .pager').textContent")
        page_pattern = f"{request_prefix}limit={page_size}&offset={page_size}"
        before_count = browser.count_requests(page_pattern)

        browser.evaluate("location.hash = '#raw'")
        browser.wait_for(away_wait, timeout=10)
        browser.evaluate(f"location.hash = '#{tab}'")
        browser.wait_for("document.querySelector('#main .pager') !== null", timeout=10)

        after_text = browser.evaluate("document.querySelector('#main .pager').textContent")
        after_count = browser.count_requests(page_pattern)

        if after_text != moved_text:
            problems.append(f"[{tab}] pager text changed across a tab round trip: "
                             f"{moved_text!r} -> {after_text!r}")
        if after_count != before_count:
            problems.append(f"[{tab}] {page_pattern} fired again on return "
                             f"({before_count} -> {after_count}) — offset restored by a "
                             f"repeat request, not from memory")

        # Дыра 4, same reasoning again: the pager's own offset is a query param
        # (`setParams({offset: ...})` in `load()`), and the round trip above went through
        # the same bare hash the dial/retrieve checks above use.
        href_now = browser.evaluate("location.href")
        if "?" not in href_now.split("#", 1)[-1] or f"offset={page_size}" not in href_now:
            problems.append(f"[{tab}] the address after the round trip does not carry its "
                             f"own offset ({page_size}): {href_now!r}")

    assert not problems, "; ".join(problems)


_STALE_AWAY_WAIT = "document.querySelector('#main input.mono.grow') !== null"   # raw tab's own input


def _round_trip_via_raw(browser: Browser, tab: str, field_sel: str):
    """Leave for the neutral '#raw' tab and come back through a BARE hash (no query
    string) — same pattern `state_survives_a_tab_round_trip` uses, and for the same
    reason: a bare hash is exactly the path where a mount-time restore has nothing but
    `ctx.state`/`VIEW_STATE` to go on, which is what both of the tests below exist to
    exercise."""
    browser.evaluate("location.hash = '#raw'")
    browser.wait_for(_STALE_AWAY_WAIT, timeout=10)
    browser.evaluate(f"location.hash = '#{tab}'")
    browser.wait_for(f"document.querySelector({json.dumps(field_sel)}) !== null", timeout=10)


@test("stale_answer_marked_after_a_tab_round_trip")
def test_stale_answer_marked_after_a_tab_round_trip(browser: Browser, base_url: str):
    """Дыра 1 (§4.1's `markAnswerStale`): a mount that restores an answer from `ctx.state`
    whose OWN params no longer match what the field/address show right now — the operator
    typed something new, no refetch yet — must say so in words and must never look green.
    Covers the three views that share this exact restore path (dial, retrieve, search): a
    guard proven on one says nothing about the other two, the same lesson §8.3's M13
    already taught about the pager offset (see `state_survives_a_tab_round_trip`'s own
    docstring).

    For each view: fire a real query A, get a real green answer; type B into the SAME
    field WITHOUT clicking anything (no refetch — `ctx.state` still holds A's answer,
    while `CURRENT_PARAMS`/the field now read B, since `fill()`'s own 'input' event runs
    the view's `syncParams` synchronously); leave the tab and come back through a bare hash.
    The remount's restore path must not present A's answer as though it were B's."""
    ts = int(time.time() * 1000)
    configs = [
        ("dial", "#main textarea", "Разложить", f"e2e_stale_dial_a_{ts}", f"e2e_stale_dial_b_{ts}"),
        ("retrieve", "#main input.grow", "Спросить озеро",
         f"e2e_stale_retrieve_a_{ts}", f"e2e_stale_retrieve_b_{ts}"),
        # Space-separated words, not an underscore-joined id: "search" is the raw BM25+vector
        # hybrid, and a random alnum token matches nothing in the corpus' FTS index — every
        # row would come back with a null bm25_rank, `hits.deadFts` would be true, and the
        # FIRST call would render '.status.warn', never the green baseline this test needs.
        # "probe" (confirmed against the real stand) is a real BM25 hit.
        ("search", "#main input.grow", "Искать",
         f"e2e stale search probe a {ts}", f"e2e stale search probe b {ts}"),
    ]
    problems = []
    for tab, field_sel, btn_text, text_a, text_b in configs:
        browser.goto(f"{base_url}/ui#{tab}")
        browser.wait_for(f"document.querySelector({json.dumps(field_sel)}) !== null", timeout=10)
        browser.fill(field_sel, text_a)
        click_button_with_text(browser, btn_text)
        browser.wait_for("document.querySelector('#main .status.ok') !== null", timeout=20)

        # The field moves on to B — no button click, no request — while ctx.state still
        # holds the answer to A.
        browser.fill(field_sel, text_b)

        _round_trip_via_raw(browser, tab, field_sel)

        field_now = browser.evaluate(f"document.querySelector({json.dumps(field_sel)}).value")
        if field_now != text_b:
            problems.append(f"[{tab}] the field itself lost its edit across the round trip: "
                             f"{field_now!r}, expected {text_b!r}")
            continue

        snapshot = browser.evaluate(
            "(() => { const boxes = [...document.querySelectorAll('#main .status')]; "
            "return { anyOk: boxes.some((b) => b.classList.contains('ok')), "
            "text: boxes.map((b) => b.textContent).join(' | ') }; })()")
        if snapshot["anyOk"]:
            problems.append(f"[{tab}] the field now reads {text_b!r} but a restored answer to "
                             f"the SUPERSEDED query {text_a!r} still shows a green '.status.ok' "
                             f"box: {snapshot['text']!r}")
        if text_a not in snapshot["text"]:
            problems.append(f"[{tab}] the restored status does not name the superseded query "
                             f"{text_a!r} in words anywhere under #main: {snapshot['text']!r}")
    assert not problems, "; ".join(problems)


@test("failure_in_a_later_mount_outranks_an_earlier_success")
def test_failure_in_a_later_mount_outranks_an_earlier_success(browser: Browser, base_url: str):
    """Дыра 2 (§4.1 defect 1 — the seq counter moved onto `ctx.state`): success in mount 1,
    a tab round trip, THEN a failure of the SAME request inside mount 2, THEN another round
    trip — mount 3 must show the failure, not the success that predates it. A SINGLE round
    trip cannot reach this: `state_survives_a_tab_round_trip` and
    `stale_answer_marked_after_a_tab_round_trip` above both fire and fail within one mount,
    so a per-mount counter (reset to 0 on every mount — the exact shape of the fixed bug)
    would still tell success and failure apart there, since both live in the SAME mount's
    own counter. The bug only surfaces once the failure happens in a mount STRICTLY LATER
    than the one that produced the success — a per-mount `let generation = 0` starts back at
    0 in the later mount too, so its own errSeq (namely 1) could tie or lose against the
    earlier mount's own okSeq (also minted from its own 0-based counter, also 1).

    Three views share this exact `ctx.state.seq` counter — dial, retrieve, AND search (see
    its own `fire()`: "on `ctx.state`, not a local `let seq = 0` — see the dial view for
    why"). A guard proven on two of the three says nothing about the third: a mutation that
    puts search's counter back on a local variable passes a `configs` list that only
    exercises dial/retrieve. Search's own SUCCESS on this corpus renders `.status.warn`, not
    `.status.ok` (a random alnum hypothesis matches nothing in the FTS index, so
    `hits.deadFts` is true — confirmed against the live index, same fact
    `stale_answer_marked_after_a_tab_round_trip`'s own configs rely on for the opposite
    reason). Asserting "no `.status.ok` in mount 3" would therefore hold trivially for
    search whether or not the bug survives — vacuous — so `success_class` below picks the
    class each view's OWN success actually uses, and the one check that is never vacuous
    for any of the three, `.status.err` present in mount 3, is asserted unconditionally."""
    ts = int(time.time() * 1000)
    configs = [
        # (tab, field_sel, btn_text, hypothesis, url_substr, success_class)
        ("dial", "#main textarea", "Разложить", f"e2e_hole2_dial_{ts}", "/dial", "ok"),
        ("retrieve", "#main input.grow", "Спросить озеро", f"e2e_hole2_retrieve_{ts}", "/retrieve", "ok"),
        ("search", "#main input.grow", "Искать", f"e2e_hole2_search_{ts}", "/search", "warn"),
    ]
    problems = []
    for tab, field_sel, btn_text, text, url_substr, success_class in configs:
        browser.goto(f"{base_url}/ui#{tab}")
        browser.wait_for(f"document.querySelector({json.dumps(field_sel)}) !== null", timeout=10)
        browser.fill(field_sel, text)
        click_button_with_text(browser, btn_text)
        browser.wait_for(f"document.querySelector('#main .status.{success_class}') !== null",
                          timeout=20)   # mount 1: success

        _round_trip_via_raw(browser, tab, field_sel)   # mount 2

        browser.evaluate("""
          (() => {
            window.__e2eOrigFetch = window.fetch;
            window.fetch = function(input, init) {
              const url = typeof input === "string" ? input : (input && input.url) || "";
              if (url.includes(%s)) return Promise.resolve(new Response("{}",
                {status: 503, headers: {"Content-Type": "application/json"}}));
              return window.__e2eOrigFetch(input, init);
            };
          })()
        """ % json.dumps(url_substr))
        try:
            # Same field value already restored by mount 2 — refiring it is the SAME
            # request, now answering 503, strictly inside this second mount.
            click_button_with_text(browser, btn_text)
            browser.wait_for("document.querySelector('#main .status.err') !== null", timeout=10)
        finally:
            browser.evaluate("(() => { if (window.__e2eOrigFetch) { "
                              "window.fetch = window.__e2eOrigFetch; delete window.__e2eOrigFetch; } })()")

        _round_trip_via_raw(browser, tab, field_sel)   # mount 3

        snapshot = browser.evaluate(
            "(() => { const boxes = [...document.querySelectorAll('#main .status')]; "
            "return { anyOk: boxes.some((b) => b.classList.contains('ok')), "
            "anyErr: boxes.some((b) => b.classList.contains('err')) }; })()")
        # Only meaningful when the view's OWN success is `.status.ok` (dial, retrieve):
        # search's success is `.status.warn` here (see the docstring above), so "no
        # .status.ok" holds regardless of the bug and would be a vacuous check.
        if success_class == "ok" and snapshot["anyOk"]:
            problems.append(f"[{tab}] a THIRD mount reads back '.status.ok' even though the "
                             f"request failed inside the SECOND mount, strictly after the "
                             f"first mount's own success")
        if not snapshot["anyErr"]:
            problems.append(f"[{tab}] a THIRD mount shows no '.status.err' at all, even though "
                             f"the request failed inside the second mount")
    assert not problems, "; ".join(problems)


@test("dial_idea_layer_matches_its_own_checkbox")
def test_dial_idea_layer_matches_its_own_checkbox(browser: Browser, base_url: str):
    """Дыра 2 (§4.1 defect 2 — `drawIdeas` as `drawDial`'s single source of truth): the
    picture must agree with the LIVE "идеи и рёбра" checkbox at draw time, not with
    whatever `graph` a past fetch left sitting in `ctx.state`. Neither reading alone is
    that truth — the checkbox alone lies right after a bare-hash round trip restores an
    unrelated `params.graph` from a PREVIOUS setting, and `graph` alone lies too (a cached
    fetch from before the box was unticked) — only `Boolean(withGraph.checked && graph)`,
    read fresh on every redraw, is. Toggling the checkbox alone never repaints the SVG
    (`renderAll`/`drawDial` only run from a fetch or a mount-time restore — see `fire()`
    and the dial's own mount code) — a tab round trip through the neutral `#raw` tab is
    what forces the redraw in both directions below, same helper `stale_answer_marked_
    after_a_tab_round_trip` uses for the same reason.

    (1) fetch WITH the box checked (idea nodes really drawn from a real graph), un-check
        it WITHOUT re-fetching, round-trip: the redraw must show zero `circle.ideanode`
        and never say "идей на картинке" — the still-cached graph must not leak through
        a now-unchecked box. The leaf dots must ALSO read the "layer off" style
        (`leafAlpha`/`leafR` in `drawDial`) even though a real graph sits in `ctx.state`
        — the other half of the same fix, and observable straight off the SVG's own `r`/
        `fill-opacity` attributes, no `getComputedStyle` needed (`drawDial` sets both as
        plain SVG attributes, not CSS).
    (2) fetch WITH the box UNCHECKED (`graph` never fetched, stays null), check it
        WITHOUT re-fetching, round-trip: zero idea nodes again (nothing to draw them
        from) and a REQUIRED non-green line naming that the shown answer was read
        without the graph — the box now reads on, the picture cannot honour that yet,
        and staying silent about it is the exact lie `renderAll`'s `else if (!graph)`
        branch exists to head off."""
    ts = int(time.time() * 1000)
    field_sel = "#main textarea"
    problems = []

    # --- (1) fetched WITH the graph, unchecked afterwards, no refetch -------------
    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for(f"document.querySelector({json.dumps(field_sel)}) !== null", timeout=10)
    browser.fill(field_sel, f"e2e_hole2_layer_a_{ts}")
    click_button_with_text(browser, "Разложить")
    browser.wait_for("document.querySelectorAll('.graphwrap svg circle.ideanode').length > 0",
                      timeout=20)

    click_checkbox_with_label(browser, "идеи и рёбра")   # un-check — no refetch
    _round_trip_via_raw(browser, "dial", field_sel)

    snap1 = browser.evaluate(
        "(() => { const svg = document.querySelector('.graphwrap svg'); "
        "const leaf = svg && svg.querySelector('g:nth-child(2) circle'); "  # gPts: leaf dots
        "const legend = document.querySelector('.legend.wrap'); "
        "return { ideaCount: document.querySelectorAll('.graphwrap svg circle.ideanode').length, "
        "mainText: document.querySelector('#main').textContent, "
        "leafR: leaf && leaf.getAttribute('r'), "
        "leafAlpha: leaf && leaf.getAttribute('fill-opacity'), "
        "legendText: legend ? legend.textContent : '' }; })()")
    if snap1["ideaCount"] != 0:
        problems.append(f"[uncheck-after-fetch] {snap1['ideaCount']} circle.ideanode still drawn "
                         f"with the box unchecked and no refetch — the stale cached graph leaked through")
    if "идей на картинке" in snap1["mainText"]:
        problems.append("[uncheck-after-fetch] the status still claims idea coverage "
                         "('идей на картинке') with the box unchecked")
    if snap1["leafR"] != "1.9" or snap1["leafAlpha"] != "0.72":
        problems.append(f"[uncheck-after-fetch] leaf dots still read the idea-layer-ON style "
                         f"(r={snap1['leafR']!r}, fill-opacity={snap1['leafAlpha']!r}) even though "
                         f"the box is unchecked and zero ideas are drawn (expected r=1.9, fill-opacity=0.72)")
    if "угол ничего не значит" not in snap1["legendText"]:
        problems.append(f"[uncheck-after-fetch] legend does not read '· угол ничего не значит' "
                         f"even though zero ideas are drawn: {snap1['legendText']!r}")
    if "крупные узлы — идеи" in snap1["legendText"]:
        problems.append(f"[uncheck-after-fetch] legend still claims 'крупные узлы — идеи' "
                         f"(the idea/edge layer caption) with the box unchecked and zero ideas drawn: "
                         f"{snap1['legendText']!r}")

    # --- (2) fetched WITHOUT the graph, checked afterwards, no refetch ------------
    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for(f"document.querySelector({json.dumps(field_sel)}) !== null", timeout=10)
    click_checkbox_with_label(browser, "идеи и рёбра")   # un-check BEFORE firing
    browser.fill(field_sel, f"e2e_hole2_layer_b_{ts}")
    click_button_with_text(browser, "Разложить")
    browser.wait_for("document.querySelector('#main .status.ok') !== null", timeout=20)

    click_checkbox_with_label(browser, "идеи и рёбра")   # re-check — no refetch
    _round_trip_via_raw(browser, "dial", field_sel)

    snap2 = browser.evaluate(
        "(() => { const boxes = [...document.querySelectorAll('#main .status')]; "
        "return { ideaCount: document.querySelectorAll('.graphwrap svg circle.ideanode').length, "
        "anyNonGreen: boxes.some((b) => !b.classList.contains('ok')), "
        "text: boxes.map((b) => b.textContent).join(' | ') }; })()")
    if snap2["ideaCount"] != 0:
        problems.append(f"[check-after-fetch] {snap2['ideaCount']} circle.ideanode drawn even "
                         f"though the graph was never fetched for this answer")
    if "прочитан без графа" not in snap2["text"]:
        problems.append(f"[check-after-fetch] the box now reads checked but nothing under #main "
                         f"says the shown answer was read without the graph: {snap2['text']!r}")
    if not snap2["anyNonGreen"]:
        problems.append("[check-after-fetch] every status box under #main reads '.status.ok' — "
                         "that reads as though the checked box's graph WAS honoured, silently")

    assert not problems, "; ".join(problems)


@test("url_reproduces_the_view")
def test_url_reproduces_the_view(browser: Browser, base_url: str):
    """§2.2/§9.3: a link carrying dial params must fill the fields WITHOUT firing a
    request — the one exception is an explicit `&run=1`, which must fire the request and
    land on the SAME numbers a direct API call gives (proving the URL, not just some
    request, drove the answer). On `retrieve`, `run=1` must NOT auto-fire anything at all
    (that route writes a line to `retrieve.jsonl` — an unsolicited write from just opening
    a link) and the tab must say so in words, not just stay silently blank.

    Hypotheses here are alnum+underscore only (no spaces/punctuation) on purpose: that
    keeps Python's `urllib.parse.quote` and the page's own `encodeURIComponent` identical
    byte for byte, so a plain substring search of the access log can tell "no request
    carried this q" apart from "a request carried it, differently encoded" without having
    to reimplement the page's own encoding to compare against it."""
    # (a) params alone: fields filled, no request fired.
    hypothesis = f"e2e_url_reproduces_dial_{int(time.time() * 1000)}"
    q = urllib.parse.quote(hypothesis)
    browser.goto(f"{base_url}/ui#dial?q={q}&k=9")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    text_now = browser.evaluate("document.querySelector('#main textarea').value")
    k_now = read_labeled_number(browser, "k")
    assert text_now == hypothesis, f"opening a dial link did not fill the hypothesis field: {text_now!r}"
    assert k_now == "9", f"opening a dial link did not fill k: {k_now!r}"
    time.sleep(1.0)   # give a wrongly-firing request time to land in the access log
    assert browser.count_requests(f"q={hypothesis}") == 0, (
        f"opening #dial?q={hypothesis}&k=9 (no run=1) fired a request carrying that q anyway")

    # (b) same link + run=1: request fires, lands on the SAME numbers a direct call gives.
    with urllib.request.urlopen(f"{base_url}/dial?q={q}&k=9", timeout=15) as r:
        direct = json.loads(r.read())
    browser.goto(f"{base_url}/ui#dial?q={q}&k=9&run=1")
    browser.wait_for("document.querySelectorAll('.graphwrap svg circle.ideanode').length > 0", timeout=20)
    status_text = status_text_sans_code(browser)
    shown_total = extract_count_before(status_text, "листьев")
    assert shown_total == direct["total"], (
        f"#dial?...&run=1 shows total={shown_total}, a direct GET /dial with the same "
        f"params says total={direct['total']}")
    rows = browser.evaluate("document.querySelectorAll('#main table.tbl tbody tr').length")
    assert rows == len(direct["hits"]), (
        f"#dial?...&run=1 hit table has {rows} rows, a direct call returned {len(direct['hits'])}")

    # (c) run=1 on retrieve: must NOT auto-fire (that route writes retrieve.jsonl), and the
    # tab must say so in its own text.
    query2 = f"e2e_url_reproduces_retrieve_{int(time.time() * 1000)}"
    q2 = urllib.parse.quote(query2)
    before = browser.count_requests("POST /retrieve")
    browser.goto(f"{base_url}/ui#retrieve?q={q2}&run=1")
    browser.wait_for("document.querySelector('#main input.grow') !== null", timeout=10)
    time.sleep(1.5)   # give a wrongly-firing POST time to land in the access log
    after = browser.count_requests("POST /retrieve")
    assert after == before, (
        f"#retrieve?q=...&run=1 fired a POST /retrieve ({before} -> {after}) — run=1 must "
        f"only ever be honoured on the dial; retrieve writes to retrieve.jsonl")
    main_text = browser.evaluate("document.querySelector('#main').textContent")
    assert "run=1" in main_text, (
        f"opening #retrieve?...&run=1 filled the field but said nothing in words about "
        f"why the request was NOT sent — expected 'run=1' to be named somewhere in #main, "
        f"got: {main_text[:400]!r}")


@test("url_reproduces_the_view_on_a_narrow_phone")
def test_url_reproduces_the_view_on_a_narrow_phone(browser: Browser, base_url: str):
    """Blocker 1 (previous gate). `url_reproduces_the_view` (above) never sets a device at
    all, so it runs at whatever width the harness starts on (desktop) — writer and reader
    agree there only because desktop's `top`/`leaves` defaults (`30`/checked) never move.
    This is deliberately a SEPARATE test, not folded into that one: it needs a 390px
    device for BOTH the write (setting the fields) and the reopen (a fresh `goto()` of the
    address just produced), and the test above does neither.

    `top=30` and a CHECKED "точки-листья" are not arbitrary — they are exactly the
    DESKTOP defaults (`30`/checked) that a regressed `syncParams` (Defect 1's original
    shape: one bare `!== 30`/`checked` comparison, blind to width) would treat as "same as
    default" and omit from the address on ANY width, narrow included. §2.3's own narrow
    defaults are different (`15`/unchecked), so reopening an address that omitted both
    keys would read back 15/unchecked at 390px — silently NOT what the operator set. The
    current fix writes both keys unconditionally on a narrow screen (see `console.html`'s
    own Defect-1 comments) precisely so this cannot happen; this guards it staying that
    way."""
    browser.set_device(390, 844, mobile=True, touch=True)
    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)

    checked = _checkbox_state(browser, "точки-листья")
    assert checked is False, (
        f"'точки-листья' defaults to checked={checked!r} at 390px — §2.3 says this should "
        f"start unchecked; the rest of this test assumes that to set up the CHANGE this "
        f"guard actually needs (unchecked -> checked)")
    click_checkbox_with_label(browser, "точки-листья")
    set_labeled_input(browser, "идей", "30")

    href = browser.evaluate("location.href")
    assert "top=30" in href, (
        f"setting идей=30 at 390px did not land in the address at all: {href!r}")
    assert "leaves=1" in href, (
        f"checking 'точки-листья' at 390px did not land in the address at all: {href!r}")

    browser.goto(href)   # brand new document, still 390px — this is `goto()`, a real load
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    top_now = read_labeled_number(browser, "идей")
    leaves_now = _checkbox_state(browser, "точки-листья")
    assert top_now == "30", (
        f"reopening a 390px link carrying top=30 came back with идей={top_now!r} — an "
        f"omitted key would default to the narrow default (15), not what was set")
    assert leaves_now is True, (
        f"reopening a 390px link carrying leaves=1 came back with 'точки-листья' checked="
        f"{leaves_now!r} — an omitted key would default to the narrow default (off), not "
        f"what was set")


@test("typing_updates_the_address")
def test_typing_updates_the_address(browser: Browser, base_url: str):
    """§8.3's M3: `url_reproduces_the_view` (above) only ever drives the URL -> fields
    direction — it opens an ALREADY-formed link, which is `parseHash`'s job, not
    `setParams`'s. Deleting the `history.replaceState` call out of `setParams` entirely
    passes every one of the other 36 checks in this file: `url_reproduces_the_view` never
    types into a field, and `state_survives_a_tab_round_trip` holds the view in
    `VIEW_STATE` memory across a switch, never touching `location.hash` at all. This is the
    missing other half: type into the field, read the address back out, then open THAT
    address as a brand new document (a real `goto`, not a same-page re-read) and check the
    fields return byte for byte.

    The typed string carries a space, an `&`, a `#` and Cyrillic together on purpose — the
    exact combination `formatHash`'s own comment says is not a byte-for-byte round trip in
    general (`URLSearchParams` re-encodes a space as `+`), so a test that only ever used
    `urllib.parse.quote`-safe alnum text (as `url_reproduces_the_view` does, deliberately,
    for a different reason) would never exercise the encoding at all."""
    hypothesis = ("typing test with a space & a hash #tag и немного кириллицы "
                  + str(int(time.time() * 1000)))
    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    browser.fill("#main textarea", hypothesis)
    set_labeled_input(browser, "k", "37")

    href = browser.evaluate("location.href")
    assert "#" in href, f"typing into the dial fields left no hash route at all: {href!r}"
    frag = href.split("#", 1)[1]
    tab_and_arg, _, query = frag.partition("?")
    assert tab_and_arg == "dial", f"typing into the dial's own fields changed the route: {frag!r}"
    parsed = urllib.parse.parse_qs(query)
    assert parsed.get("q") == [hypothesis], (
        f"the address does not carry the typed hypothesis byte for byte: "
        f"q={parsed.get('q')!r}, typed {hypothesis!r} (full hash: {frag!r})")
    assert parsed.get("k") == ["37"], f"the address does not carry the typed k: {frag!r}"

    # Open the EXACT address just read back, as a brand new document — this is `goto()`,
    # which forces a real navigation (see its own docstring), so a pass here can only be
    # `parseHash` restoring the fields on load, never `VIEW_STATE` still holding them.
    browser.goto(href)
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    text_now = browser.evaluate("document.querySelector('#main textarea').value")
    k_now = read_labeled_number(browser, "k")
    assert text_now == hypothesis, (
        f"reopening the address the page itself just produced lost the hypothesis: "
        f"{text_now!r}, typed {hypothesis!r}")
    assert k_now == "37", f"reopening the address the page itself just produced lost k: {k_now!r}"


@test("timers_die_with_the_tab")
def test_timers_die_with_the_tab(browser: Browser, base_url: str):
    """§2.2/§4.2: the ingest queue's autorefresh `setInterval` must die when its tab is
    left, or it is a background timer hitting the server from a tab nobody is looking at —
    and once `VIEW_STATE` keeps the ingest view mounted-in-memory across a switch, a timer
    that does not unregister itself leaks one MORE interval every time the operator
    revisits the tab. Turns autorefresh on, waits for two real ticks off the server's OWN
    access log (a background timer is not something Chrome's readyState can wait on), then
    leaves for another tab and waits comfortably longer than one period for a THIRD tick
    that must never come."""
    PERIOD_S = 4.0   # console.html's own "автообновление 4 с" (setInterval(..., 4000))
    browser.goto(f"{base_url}/ui#ingest")
    browser.wait_for("document.querySelector('#main input[type=checkbox]') !== null", timeout=10)

    before = browser.count_requests("GET /ingest/jobs")
    browser.click("#main input[type=checkbox]")   # the tab's only checkbox: "автообновление 4 с"
    after_two_ticks = wait_for_request_count(browser, "GET /ingest/jobs", before + 2,
                                              timeout=PERIOD_S * 3 + 5)

    browser.evaluate("location.hash = '#dial'")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    time.sleep(PERIOD_S * 2.5)   # comfortably longer than one period, spent on another tab
    after_leaving = browser.count_requests("GET /ingest/jobs")

    assert after_leaving == after_two_ticks, (
        f"GET /ingest/jobs kept firing after leaving the 'ингест' tab: {after_two_ticks} "
        f"requests when leaving it, {after_leaving} now, {PERIOD_S * 2.5:.0f}s later — the "
        f"autorefresh timer outlived its tab")


@test("failed_call_returns_the_button")
def test_failed_call_returns_the_button(browser: Browser, base_url: str):
    """§4.2/§8.4: a rejected `/dial` or `/retrieve` fetch must not leave its button stuck
    grey forever — nothing on screen would invite a retry. This does NOT exercise `guard`'s
    own `finally` on its throw path, and its docstring used to claim it did: console.html's
    `run()` catches every error itself (an `ApiError`, a plain network reject, even a
    render throw past a successful fetch) and never rethrows, so the `fn(event)` `guard`
    awaits here never actually throws either — `guard`'s `finally` runs on the exact same
    non-exceptional path it takes on success. What this proves is `run()`'s own guarantee
    (a failed call still leaves the button clickable again), not a direct test of `guard`'s
    `finally` catching an exception — that lives in the page's own `?selftest=1` block,
    which can synchronously make a wrapped `fn` throw past `run()` and assert the button
    comes back anyway. Checked on both action buttons this guarantee covers; proven on one
    says nothing about the other, same reasoning as every other per-view test above."""
    configs = [
        ("dial", "#main textarea", f"e2e_failed_call_dial_{int(time.time() * 1000)}",
         "Разложить", "/dial"),
        ("retrieve", "#main input.grow", f"e2e_failed_call_retrieve_{int(time.time() * 1000)}",
         "Спросить озеро", "/retrieve"),
    ]
    problems = []
    for tab, field_sel, text, btn_text, url_substr in configs:
        browser.goto(f"{base_url}/ui#{tab}")
        browser.wait_for(f"document.querySelector({json.dumps(field_sel)}) !== null", timeout=10)
        browser.fill(field_sel, text)

        browser.evaluate("""
          (() => {
            window.__e2eOrigFetch = window.fetch;
            window.fetch = function(input, init) {
              const url = typeof input === "string" ? input : (input && input.url) || "";
              if (url.includes(%s)) return Promise.reject(new TypeError("e2e-injected: offline"));
              return window.__e2eOrigFetch(input, init);
            };
          })()
        """ % json.dumps(url_substr))
        try:
            click_button_with_text(browser, btn_text)
            browser.wait_for("document.querySelector('#main .status.err') !== null", timeout=10)
        finally:
            browser.evaluate("(() => { if (window.__e2eOrigFetch) { "
                              "window.fetch = window.__e2eOrigFetch; delete window.__e2eOrigFetch; } })()")

        disabled = browser.evaluate(
            "(() => { const btns = [...document.querySelectorAll('#main button')]; "
            "const b = btns.find((x) => x.textContent.trim() === " + json.dumps(btn_text) + "); "
            "return b ? b.disabled : null; })()")
        if disabled is None:
            problems.append(f"[{tab}] no button with text {btn_text!r} found after the failed call")
        elif disabled:
            problems.append(f"[{tab}] '{btn_text}' is still disabled after its call failed — "
                             f"stuck grey forever, a new silent failure of its own")
    assert not problems, "; ".join(problems)


@test("confirm_is_asked_before_expensive")
def test_confirm_is_asked_before_expensive(browser: Browser, base_url: str):
    """§4.2/§8.3's M10: three costly/destructive actions ('Судить', 'Выгрузить в
    Obsidian', 'Фаза 2') must ask `confirm()` before calling their route, and Cancel must
    send NOTHING, checked against the server's OWN access log rather than trusting the
    page's claim. `re.search(r"\\d", message)` alone (the old check) passes on a FABRICATED
    zero — "идей 0, тезисов 0, источников 0" contains a digit whether or not it is real,
    exactly the class of lie §4.1 bans — so this reads the reference numbers straight off
    `GET /stats` and asserts the dialog names THOSE, not just some digit:

      - 'Выгрузить в Obsidian' must quote the real идей/тезисов/источников counts /stats
        answers with right now (normalised for the page's own thousands separator — it
        prints via `toLocaleString("ru-RU")`, a non-breaking space, not a plain one).
      - 'Судить' with the id field EMPTY must say the real (zero) count of explicit ids in
        those exact words — not just a stray digit that happens to be a literal 0.
      - 'Судить' with the id field FILLED must name the REAL count of ids just typed, so a
        mutation that hardcodes some other number in that branch cannot hide behind "some
        digit is present, still".

    Only ever cancels: this suite must not actually run a 35B judge pass, export the
    vault, or write phase 2 into the shared graph as a side effect of testing that the
    GUARD exists — the guard's whole job is to make that an operator's deliberate choice,
    and accepting here to "prove" it fires would defeat the reason this test exists.

    Dialog interception is CDP's `Page.javascriptDialogOpening` / `handleJavaScriptDialog`
    (see `Browser.wait_for_dialog`/`handle_dialog`) — a bare Python `assert` here cannot
    see a real `window.confirm()` at all, since it blocks the page's own JS thread until
    answered.

    Дыра 3: 'Фаза 2's own dialog quotes `pending_lines` straight off `GET /ingest/staging`
    (console.html's own comment at the call site says so) — checked here against a MOCKED,
    fabricated, nonzero `pending_lines`, never against the real stand's, which happens to be
    0 right now: a mutation that simply zeroed the dialog's own number (or hardcoded any
    other single literal) would be indistinguishable from correct against a real 0, and
    prove nothing about whether the page is actually reading staging's own field."""
    with urllib.request.urlopen(f"{base_url}/stats", timeout=15) as r:
        stats = json.loads(r.read())

    FAKE_PENDING = 12345
    staging_ident = _install_url_override(browser, "/ingest/staging", _response_js(200, {
        "lines": FAKE_PENDING, "cursor": "e2e-fake-cursor",
        "pending_lines": FAKE_PENDING, "sources": [],
    }))

    def number_after(text, label):
        """The integer immediately after a literal label word (e.g. `"идей 12 345,"`),
        the mirror image of `extract_count_before` — this dialog's own template puts the
        label BEFORE its number, not after."""
        m = re.search(re.escape(label) + r"\s+([\d" + _THOUSANDS_SEP[1:-1] + r"]+)", text)
        assert m, f"no number right after {label!r} in: {text!r}"
        return int(re.sub(_THOUSANDS_SEP, "", m.group(1)))

    problems = []
    try:
        browser.goto(f"{base_url}/ui#ingest")
        browser.wait_for("document.querySelector('#main .panel') !== null", timeout=10)
        # LAST_STATS (the number 'Выгрузить в Obsidian' actually quotes) is filled by the
        # page's own boot-time refreshStrip() — give that its own request time to land
        # instead of racing it, independently of however long the steps below happen to take.
        browser.wait_for("typeof LAST_STATS !== 'undefined' && LAST_STATS !== null", timeout=15)
        # The mocked /ingest/staging (installed above, before this goto) must actually have
        # landed and rendered before 'Фаза 2' is clicked below — this badge is the tab's own,
        # real confirmation that it did, not an assumption.
        browser.wait_for(
            "[...document.querySelectorAll('#main .badge')].some((b) => "
            "b.textContent.includes(" + json.dumps(f"ждёт фазы 2: {FAKE_PENDING}") + "))",
            timeout=10)

        _confirm_is_asked_before_expensive_steps(browser, base_url, stats, FAKE_PENDING,
                                                  number_after, problems)
    finally:
        browser.remove_init_script(staging_ident)
    assert not problems, "; ".join(problems)


def _confirm_is_asked_before_expensive_steps(browser, base_url, stats, FAKE_PENDING,
                                              number_after, problems):
    """The three dialogs themselves — split out of `test_confirm_is_asked_before_expensive`
    only so that function's own `try/finally` (removing the '/ingest/staging' mock) reads as
    one screenful, not because this needs to be reusable."""

    def judge_and_cancel(note):
        before = browser.count_requests("POST /admin/trust")
        click_button_with_text(browser, "Судить")
        try:
            dialog = browser.wait_for_dialog(timeout=5)
        except TimeoutError:
            problems.append(f"[Судить{note}] no confirm() dialog opened before POST /admin/trust")
            return None
        message = dialog.get("message", "")
        browser.handle_dialog(accept=False)   # Cancel — never accept, see docstring
        time.sleep(0.5)   # give a wrongly-firing request time to land in the access log
        after = browser.count_requests("POST /admin/trust")
        if after != before:
            problems.append(f"[Судить{note}] cancelling still sent POST /admin/trust "
                             f"({before} -> {after})")
        return message

    # (a) empty field: the real count of explicit ids is 0 — the dialog must say so in
    # these exact words, not merely contain SOME digit (a hardcoded "0" would too).
    msg_empty = judge_and_cancel(" (id field empty)")
    if msg_empty is not None and "не указано (0)" not in msg_empty:
        problems.append(f"[Судить (id field empty)] expected the literal explicit-id count "
                         f"'не указано (0)' in the dialog text, got: {msg_empty!r}")

    # (b) filled field: the dialog must name the REAL count of ids just typed — a mutation
    # that swaps `ids.length` for a different number would survive (a) above (still 0
    # there) and would survive the old bare-digit check, but not this.
    ids_typed = ["e2e_a", "e2e_b", "e2e_c"]
    set_labeled_input(browser, "судить идеи", ", ".join(ids_typed))
    msg_filled = judge_and_cancel(" (id field filled)")
    if msg_filled is not None:
        m = re.search(r"над\s+(\d+)\s+указанными", msg_filled)
        if not m or int(m.group(1)) != len(ids_typed):
            problems.append(f"[Судить (id field filled)] expected 'над {len(ids_typed)} "
                             f"указанными' in the dialog text, got: {msg_filled!r}")
    set_labeled_input(browser, "судить идеи", "")   # leave the field clean for what follows

    # --- 'Выгрузить в Obsidian': dialog must quote the REAL /stats numbers ---
    before = browser.count_requests("POST /vault/export")
    click_button_with_text(browser, "Выгрузить в Obsidian")
    try:
        dialog = browser.wait_for_dialog(timeout=5)
        message = dialog.get("message", "")
        for label, real in [("идей", stats["ideas"]), ("тезисов", stats["theses"]),
                             ("источников", stats["sources"])]:
            shown = number_after(message, label)
            if shown != real:
                problems.append(f"[Выгрузить в Obsidian] dialog says {label} {shown}, "
                                 f"GET /stats says {real} — {message!r}")
        browser.handle_dialog(accept=False)   # Cancel — never accept, see docstring
        time.sleep(0.5)   # give a wrongly-firing request time to land in the access log
        after = browser.count_requests("POST /vault/export")
        if after != before:
            problems.append(f"[Выгрузить в Obsidian] cancelling still sent POST /vault/export "
                             f"({before} -> {after})")
    except TimeoutError:
        problems.append("[Выгрузить в Obsidian] no confirm() dialog opened before POST /vault/export")

    # --- 'Фаза 2': the dialog's own number must be the SAME one GET /ingest/staging just
    # reported — checked against the mocked, fabricated FAKE_PENDING installed by the
    # caller, never against the real stand's own pending_lines (0 right now), which a
    # mutation that simply zeroed or hardcoded the dialog's number would pass by accident.
    before = browser.count_requests("POST /ingest/phase2")
    click_button_with_text(browser, "Фаза 2 (staging → граф)")
    try:
        dialog = browser.wait_for_dialog(timeout=5)
        message = dialog.get("message", "")
        shown = extract_count_before(message, "строк")
        if shown != FAKE_PENDING:
            problems.append(f"[Фаза 2] dialog says {shown} строк(и), mocked GET "
                             f"/ingest/staging reported pending_lines={FAKE_PENDING} — {message!r}")
        browser.handle_dialog(accept=False)   # Cancel — never accept, see docstring
        time.sleep(0.5)   # give a wrongly-firing request time to land in the access log
        after = browser.count_requests("POST /ingest/phase2")
        if after != before:
            problems.append(f"[Фаза 2] cancelling still sent POST /ingest/phase2 "
                             f"({before} -> {after})")
    except TimeoutError:
        problems.append("[Фаза 2] no confirm() dialog opened before POST /ingest/phase2")


# ============================================================ §4.2: double-click guard

@test("single_request_on_double_click")
def test_single_request_on_double_click(browser: Browser, base_url: str):
    """§4.2: `guard(fn)` must stop a second click on 'Разложить' before the first answer
    lands from firing a second `GET /dial` — was `expected_fail=True` (no debounce/disable
    shipped yet); now the guard exists, this must PASS (see `test()`'s XFAIL/XPASS
    contract: an XFAIL that starts passing without dropping the flag fails the run on
    purpose, so this line is the proof the flag was actually dropped, not forgotten)."""
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


@test("single_request_on_double_click_retrieve")
def test_single_request_on_double_click_retrieve(browser: Browser, base_url: str):
    """Same guard, the retrieve tab's own 'Спросить озеро' — §4.2 says the guard wraps
    every `button.act`, not just the dial's, and a double POST here is worse than a double
    GET on the dial: it is a second, real line in `retrieve.jsonl` polluting the A/B log
    the project measures against, not just a wasted read."""
    query = "guard the second click before the first retrieve answer lands"
    browser.goto(f"{base_url}/ui#retrieve")
    browser.wait_for("document.querySelector('#main input.grow') !== null", timeout=10)
    browser.fill("#main input.grow", query)

    before = browser.count_requests("POST /retrieve")
    click_button_with_text(browser, "Спросить озеро")
    click_button_with_text(browser, "Спросить озеро")
    browser.wait_for("document.querySelector('#main .status.ok') !== null", timeout=20)
    time.sleep(0.5)   # let a second in-flight request, if the page fired one, finish and log
    made = browser.count_requests("POST /retrieve") - before

    assert made == 1, (
        f"double-clicking 'Спросить озеро' fired {made} POST /retrieve requests, want 1 — "
        f"no guard against a second click before the first answer lands")


@test("phone_no_horizontal_scroll")
def test_phone_no_horizontal_scroll(browser: Browser, base_url: str):
    """§0.3: the page used to run off the side of a phone screen, and
    `document.documentElement.scrollWidth` alone is blind to nearly all of it — see
    `measure_overflow`'s docstring. Measured by hand at 390x844 before this guard was
    written: nav.tabs clientWidth 390 / scrollWidth 785, #ideas .scroller 352 / 713,
    header.bar bottom ~103px against the CSS-hardcoded `nav.tabs{top:53px}`, and no
    `@media` rule under 620px or 430px anywhere in the file. §0.3/§2.3 have since landed
    (one sticky `.chrome` wrapping header+tabs, breakpoints at 900/620/430) and this guard
    is a plain test now, not `expected_fail` — see `test()` docstring for what XFAIL/XPASS
    meant while it still was one. If this ever fails again, that is a real regression."""
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


@test("phone_header_and_tabs_share_one_sticky_container")
def test_phone_header_and_tabs_share_one_sticky_container(browser: Browser, base_url: str):
    """§0.3/§2.3: `nav.tabs{top:53px}` used to be a SECOND, independent sticky element,
    pinned to exactly the single-line header's own height — the moment the header
    wrapped to more than one line (narrow width), the tab strip pinned itself UNDER the
    header instead of below it. The fix folds both into one `.chrome{position:sticky;
    top:0}` wrapper, so there is no offset to compute or get wrong at all.

    Checked structurally, not just "do they overlap on screen right now" (that overlap
    check already lives in `phone_no_horizontal_scroll`, across all nine tabs): `.chrome`
    really contains both elements, `.chrome` itself is the one carrying `position:sticky;
    top:0`, and `nav.tabs` does NOT have its own `position:sticky` any more. That last
    assertion is the one a `header_nav_overlap`-style pixel check alone cannot make: two
    independent stickies that happen to line up at ONE measured width are still the old
    bug's shape, just not tripped by that particular number."""
    browser.set_device(390, 844, mobile=True, touch=True)
    browser.goto(f"{base_url}/ui#raw")
    browser.wait_for("document.querySelector('#main .panel') !== null", timeout=10)

    geo = browser.evaluate("""
      (() => {
        const chrome = document.querySelector('.chrome');
        const header = document.querySelector('header.bar');
        const nav = document.querySelector('nav.tabs');
        if (!chrome || !header || !nav) return null;
        return {
          containsBoth: chrome.contains(header) && chrome.contains(nav),
          chromePosition: getComputedStyle(chrome).position,
          chromeTop: getComputedStyle(chrome).top,
          navPosition: getComputedStyle(nav).position,
          headerBottom: header.getBoundingClientRect().bottom,
          navTop: nav.getBoundingClientRect().top,
        };
      })()
    """)
    assert geo, "missing .chrome / header.bar / nav.tabs — cannot check the sticky wrapper at all"
    assert geo["containsBoth"], (
        ".chrome does not contain both header.bar and nav.tabs — they are independent "
        "elements again, exactly the shape the top:53px bug had")
    assert geo["chromePosition"] == "sticky" and geo["chromeTop"] == "0px", (
        f".chrome is not the sticky wrapper: position={geo['chromePosition']!r} "
        f"top={geo['chromeTop']!r}")
    assert geo["navPosition"] != "sticky", (
        f"nav.tabs has its own position:sticky ({geo['navPosition']!r}) again — that is "
        "the old bug's exact shape even if it does not overlap on this one measurement")
    assert geo["headerBottom"] <= geo["navTop"] + 1, (
        f"header.bar bottom {geo['headerBottom']}px is past nav.tabs top {geo['navTop']}px "
        "— the tab strip is drawn under the header, not below it")


def cardable_table_check(browser: Browser) -> dict:
    """Reads back, off whichever `table.tbl.cardable` is on screen, the two facts §2.3's
    card reflow depends on: the row is really a CSS block (not a table row any more), and
    the FIRST `<td>` — always a real, always-headed column on all four cardable tables,
    unlike a trailing action column `labelCells()` deliberately leaves unset — actually
    carries `data-label` and prints it through the `::before` pseudo-element, rather than
    just sitting unused in the DOM. Pinned to `td:first-child` specifically (not "some
    cell has a label", which a bug dropping only the FIRST column's label would still
    satisfy via any later column) — reads the label straight off the live table instead of
    asserting a specific Russian column name, so a column reorder cannot silently break
    this alongside the feature it is checking."""
    return browser.evaluate("""
      (() => {
        const table = document.querySelector('#main table.tbl.cardable');
        if (!table) return { table: false };
        const tr = table.querySelector('tbody tr');
        if (!tr) return { table: true, row: false };
        const td = tr.querySelector('td:first-child');
        return {
          table: true, row: true,
          rowDisplay: getComputedStyle(tr).display,
          label: td ? (td.dataset.label ?? null) : null,
          beforeContent: td ? getComputedStyle(td, '::before').content : null,
        };
      })()
    """)


@test("phone_content_tables_become_cards")
def test_phone_content_tables_become_cards(browser: Browser, base_url: str):
    """§2.3: the four CONTENT tables — dial hits, "Идеи", "Тезисы", "Источники" — reflow
    row-by-row into cards at <=430px, each `<td>` printing its own column's real `<th>`
    text via `data-label`/`::before` (`labelCells()`), not a second hand-written label
    that can drift from the header. Checked on all four, against the live stand's own
    data (real ideas/theses/sources, not a fixture — see `LakeServer.start`'s docstring
    for which routes `--mock` leaves untouched), via `cardable_table_check` so the same
    two facts (block-mode row, label actually prints) are asserted identically on each."""
    browser.set_device(390, 844, mobile=True, touch=True)

    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    browser.fill("#main textarea", "e2e phone card-table probe")
    click_button_with_text(browser, "Разложить")
    browser.wait_for("document.querySelector('.graphwrap svg') !== null", timeout=20)
    browser.wait_for("document.querySelector('#main table.tbl.cardable tbody tr') !== null", timeout=10)
    checks = {"dial hits": cardable_table_check(browser)}

    for tab_id, label in (("ideas", "Идеи"), ("theses", "Тезисы"), ("sources", "Источники")):
        browser.goto(f"{base_url}/ui#{tab_id}")
        browser.wait_for("document.querySelector('#main table.tbl.cardable tbody tr') !== null",
                          timeout=15)
        checks[label] = cardable_table_check(browser)

    for name, c in checks.items():
        assert c["table"], f"[{name}] no table.tbl.cardable found on screen at 390px"
        assert c["row"], f"[{name}] table.tbl.cardable has no tbody tr to check at all"
        assert c["rowDisplay"] == "block", (
            f"[{name}] tbody tr computed display is {c['rowDisplay']!r} at 390px, want "
            f"'block' (card mode)")
        assert c["label"], (
            f"[{name}] first cell has no data-label — the column's own name is gone, "
            f"not just visually hidden")
        assert c["beforeContent"] not in (None, "none", "normal", '""'), (
            f"[{name}] td::before content is {c['beforeContent']!r} — data-label is set "
            f"but never printed")


@test("phone_service_tables_stay_scrollable_not_cards")
def test_phone_service_tables_stay_scrollable_not_cards(browser: Browser, base_url: str):
    """§2.3: "служебные (очередь заданий, отказы арбитра) — оставить скролл, туда с
    телефона не смотрят" — the ingest tab's three tables (jobs, staging, pending-link)
    must NOT get `cardable` at 390px: real `<tr>` rows inside `.scroller`, with the
    general <430px `table-layout:fixed` rule (not the cardable reflow) keeping them from
    overflowing sideways. Checked against the live queue (`--mock` still serves real
    `/ingest/*` — see `LakeServer.start`'s docstring), so staging/pending-link may or may
    not have a table at all on a given run (both render a plain "empty" status div when
    there is nothing to show) — those two are checked only when a table is actually
    present; `GET /ingest/jobs` always renders one (even with zero rows) and is required
    to be checked, so an empty run can never make this test vacuous."""
    browser.set_device(390, 844, mobile=True, touch=True)
    browser.goto(f"{base_url}/ui#ingest")
    jobs_panel = panel_js("GET /ingest/jobs")
    browser.wait_for(f"(() => {{ const p = {jobs_panel}; return p && "
                      "p.querySelector('table.tbl') !== null; })()", timeout=15)

    geo = browser.evaluate("""
      (() => {
        const headings = ["GET /ingest/jobs", "GET /ingest/staging — точка приёмки",
                           "GET /ingest/pending-link — отказы арбитра"];
        const panels = [...document.querySelectorAll('#main .panel')];
        return headings.map((heading) => {
          const p = panels.find((x) => { const h = x.querySelector('h2');
            return h && h.textContent.trim() === heading; });
          if (!p) return { heading, panel: false, table: false };
          const table = p.querySelector('table.tbl');
          if (!table) return { heading, panel: true, table: false };
          const scroller = p.querySelector('.scroller');
          return {
            heading, panel: true, table: true,
            cardable: table.classList.contains('cardable'),
            tableLayout: getComputedStyle(table).tableLayout,
            overflowX: scroller ? scroller.scrollWidth > scroller.clientWidth + 1 : null,
          };
        });
      })()
    """)
    assert geo, "no ingest service-table panels found at all"
    for row in geo:
        assert row["panel"], f"[{row['heading']}] panel not found on #ingest at all"
        if not row["table"]:
            continue   # empty-state status div instead of a table — nothing to check here
        assert row["cardable"] is False, (
            f"[{row['heading']}] table.tbl has 'cardable' at 390px — service tables must "
            f"stay real tables, not reflow into cards")
        assert row["tableLayout"] == "fixed", (
            f"[{row['heading']}] table-layout is {row['tableLayout']!r} at 390px, want 'fixed'")
        assert row["overflowX"] is False, (
            f"[{row['heading']}] .scroller overflows sideways at 390px: {row}")

    jobs_row = next(r for r in geo if r["heading"] == "GET /ingest/jobs")
    assert jobs_row["table"], (
        "GET /ingest/jobs rendered no table at all — cannot prove anything about service "
        "tables staying scrollable on this run")


@test("phone_buttons_meet_minimum_touch_target")
def test_phone_buttons_meet_minimum_touch_target(browser: Browser, base_url: str):
    """§2.3: at max-width:620, `button.act`/`button.ghost` get `min-height:44px`, and
    `.ghost.sm` (measured smaller before this pass — the pager arrows, each row's own
    "открыть") additionally gets `min-width:44px` (WCAG 2.5.5). Checked on real rendered
    buttons a phone user would actually tap — the dial's "Разложить" (`button.act`), and
    the ideas pager's arrows plus a row's own "открыть" (`button.ghost.sm`) — not a CSS
    rule read out of the stylesheet, which would still pass even if the selector had
    stopped matching the real markup."""
    browser.set_device(390, 844, mobile=True, touch=True)

    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    act_rect = browser.evaluate("""
      (() => { const b = [...document.querySelectorAll('#main button.act')]
                 .find((x) => x.textContent.trim() === 'Разложить');
                if (!b) return null; const r = b.getBoundingClientRect();
                return { width: r.width, height: r.height }; })()
    """)
    assert act_rect, "no 'Разложить' button.act found on #dial at 390px"
    assert act_rect["width"] >= 44 and act_rect["height"] >= 44, (
        f"'Разложить' (button.act) is {act_rect['width']}x{act_rect['height']} at 390px, "
        f"want >=44x44")

    browser.goto(f"{base_url}/ui#ideas")
    browser.wait_for("document.querySelector('#main table.tbl.cardable tbody tr') !== null",
                      timeout=15)
    sm_rects = browser.evaluate("""
      [...document.querySelectorAll('#main button.ghost.sm')].map((b) => {
        const r = b.getBoundingClientRect();
        return { text: b.textContent.trim(), width: r.width, height: r.height };
      })
    """)
    assert sm_rects, "no button.ghost.sm found on #ideas at 390px (pager arrows / 'открыть')"
    for r in sm_rects:
        assert r["width"] >= 44 and r["height"] >= 44, (
            f"button.ghost.sm {r['text']!r} is {r['width']}x{r['height']} at 390px, "
            f"want >=44x44")


@test("phone_dial_height_scales_with_width_not_just_window_height")
def test_phone_dial_height_scales_with_width_not_just_window_height(browser: Browser, base_url: str):
    """§2.3: `h = min(w, 0.7 * innerHeight)`, where `w` is the graph wrapper's OWN
    `clientWidth` (`console.html:1115`: `const w = wrap.clientWidth || 1100`) — before
    this pass `h` came from window height alone (§0.3, `console.html:736`), so any two
    viewports that share `innerHeight` drew the SAME square regardless of width.

    Proven with exactly that pair: 390x844 and 900x844 share `innerHeight`, so a
    height-only bug draws an IDENTICAL side on both. The formula instead predicts
    `min(wrapWidth_narrow, round(0.7*844)=591)` and `min(wrapWidth_wide, 591)` —
    different, since `wrapWidth_narrow` is well under 591 and `wrapWidth_wide` is well
    over it — and every number is read straight off the live DOM (`.graphwrap`'s own
    `clientWidth`, `svg.viewBox.baseVal`), never hardcoded, so a slightly different
    Chrome chrome/padding cannot make this assert on a number the page never actually
    computed.

    Blocker 2 (previous gate): this used to read ONLY the `viewBox` ATTRIBUTE, never the
    rendered box — green whether the picture actually matched that coordinate system or
    not. Mutation G8 (`svg.graph{height:min(70vh,720px)}` -> `height:auto`,
    console.html:237) changes what is actually drawn and sailed straight through. It
    still would on the DIAL's own svg alone: Defect 2's fix set an inline
    `style="height:...px"` on that one element (console.html:1185), and an inline style
    always outranks a class rule, so the dial's rendered box stays correct no matter what
    the shared class says — proving nothing about whether the class rule itself still
    holds. The graph-explorer view's OWN `svg.graph` (console.html:2731) has no such
    override and trusts the class rule alone, which is exactly where a broken class
    would show up as a mismatched box. Both are checked here: the dial's box against its
    viewBox (guards the inline-style fix), and the graph-explorer's box against ITS
    viewBox (guards the shared class rule the dial's override can no longer speak for)."""
    def draw(w, h):
        browser.set_device(w, h, mobile=True, touch=True)
        browser.goto(f"{base_url}/ui#dial")
        browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
        browser.fill("#main textarea", "e2e phone dial-square probe")
        click_button_with_text(browser, "Разложить")
        browser.wait_for("document.querySelector('.graphwrap svg') !== null", timeout=20)
        return browser.evaluate("""
          (() => {
            const svg = document.querySelector('.graphwrap svg');
            const vb = svg.viewBox.baseVal;
            const box = svg.getBoundingClientRect();
            const wrapWidth = document.querySelector('.graphwrap').clientWidth;
            return { w: vb.width, h: vb.height, wrapWidth, innerHeight: window.innerHeight,
                     boxW: box.width, boxH: box.height };
          })()
        """)

    narrow = draw(390, 844)
    wide = draw(900, 844)

    for label, geo in (("390x844", narrow), ("900x844", wide)):
        assert geo["w"] == geo["wrapWidth"], (
            f"[{label}] viewBox width {geo['w']} != .graphwrap clientWidth {geo['wrapWidth']}")
        want_h = min(geo["wrapWidth"], round(geo["innerHeight"] * 0.7))
        assert geo["h"] == want_h, (
            f"[{label}] viewBox height {geo['h']} != min(w={geo['wrapWidth']}, "
            f"round(0.7*innerHeight)={round(geo['innerHeight'] * 0.7)}) = {want_h}")
        # Blocker 2: the ATTRIBUTE alone says nothing about what is actually on screen —
        # only the rendered box does.
        assert abs(geo["boxW"] - geo["w"]) <= 1 and abs(geo["boxH"] - geo["h"]) <= 1, (
            f"[{label}] dial svg's rendered box {geo['boxW']}x{geo['boxH']} != its own "
            f"viewBox {geo['w']}x{geo['h']} — the picture on screen no longer matches "
            f"the coordinate system it is drawn in")

    assert narrow["h"] != wide["h"], (
        f"390x844 and 900x844 share innerHeight but drew the SAME side ({narrow['h']}px) "
        f"— the dial is still sized from window height alone, ignoring width")

    # Blocker 2, continued: the dial's own box is immune to a broken shared class rule
    # (its inline style always wins), so the class rule itself is only ever provable on
    # the OTHER svg.graph, the graph-explorer's — same selector, different tab.
    with urllib.request.urlopen(f"{base_url}/ideas?limit=1&offset=0", timeout=10) as r:
        seed_idea_id = json.load(r)["items"][0]["id"]
    browser.goto(f"{base_url}/ui#graph")
    browser.wait_for("document.querySelector('#main input.grow') !== null", timeout=10)
    browser.fill("#main input.grow", seed_idea_id)
    click_button_with_text(browser, "Нарисовать")
    browser.wait_for("document.querySelector('.graphwrap svg') !== null", timeout=20)
    explorer = browser.evaluate("""
      (() => {
        const svg = document.querySelector('.graphwrap svg');
        const vb = svg.viewBox.baseVal;
        const box = svg.getBoundingClientRect();
        return { w: vb.width, h: vb.height, boxW: box.width, boxH: box.height };
      })()
    """)
    assert abs(explorer["boxW"] - explorer["w"]) <= 1 and abs(explorer["boxH"] - explorer["h"]) <= 1, (
        f"graph-explorer svg's rendered box {explorer['boxW']}x{explorer['boxH']} != its "
        f"own viewBox {explorer['w']}x{explorer['h']} — svg.graph's shared CSS height "
        f"rule (console.html:237) no longer matches what drawGraph() computed")


@test("phone_leaves_off_default_status_matches_dial_total")
def test_phone_leaves_off_default_status_matches_dial_total(browser: Browser, base_url: str):
    """§2.3: leaves default OFF under 430px width, and the status must say so IN WORDS,
    naming the actual screen width — while the `total` it quotes stays the server's own
    count, not the count of dots actually drawn (leaves hidden draws exactly zero of
    them). Checked three separate ways, each a distinct way this could quietly lie:

    1. the checkbox itself starts UNCHECKED at 390px, not just "happens to draw nothing"
       for some unrelated reason;
    2. zero leaf circles are actually appended to the SVG (`gPts`, the 2nd `<g>` in
       z-order — `svg.append(gRings, gPts, gEdges, gIdeas, gHits, gLab)`, confirmed by
       `dial_marks_geometry`'s own docstring);
    3. the number quoted in BOTH the "leaves hidden" note and the main status line is
       GET /dial's own `total`, fetched directly and independently of the page (same
       pattern as `test_dial_answers`) — NOT `0` (drawn dots) and not anything else the
       page might compute client-side."""
    browser.set_device(390, 844, mobile=True, touch=True)
    hypothesis = "e2e phone leaves-off probe"

    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    checked = _checkbox_state(browser, "точки-листья")
    assert checked is False, (
        f"'точки-листья' defaults to checked={checked!r} at 390px width, want unchecked")

    k = read_labeled_number(browser, "k")
    browser.fill("#main textarea", hypothesis)
    click_button_with_text(browser, "Разложить")
    browser.wait_for("document.querySelector('.graphwrap svg') !== null", timeout=20)
    browser.wait_for("document.querySelector('#main .status.warn, #main .status.ok') !== null",
                      timeout=20)

    leaf_dots = browser.evaluate("document.querySelectorAll('svg > g:nth-of-type(2) circle').length")
    assert leaf_dots == 0, (
        f"'точки-листья' is unchecked but {leaf_dots} leaf circles were drawn anyway")

    note = browser.evaluate(
        "[...document.querySelectorAll('#main .status')].map((e) => e.textContent)"
        ".find((t) => t.includes('скрыты')) ?? null")
    assert note, "no status line says leaves are hidden, even though the checkbox is unchecked"
    assert "390" in note, f"leaves-hidden note does not name the actual screen width (390): {note!r}"

    q = urllib.parse.quote(hypothesis)
    with urllib.request.urlopen(f"{base_url}/dial?q={q}&k={k}", timeout=15) as r:
        data = json.loads(r.read())

    note_total = extract_count_before(note, "листьев")
    assert note_total == data["total"], (
        f"leaves-hidden note claims {note_total} листьев, GET /dial says total="
        f"{data['total']}: {note!r}")
    assert note_total != leaf_dots, (
        f"leaves-hidden note's count ({note_total}) equals the number of dots actually "
        f"drawn ({leaf_dots}) — looks like it is quoting drawn dots, not the server's total")

    main_status_total = extract_count_before(status_text_sans_code(browser), "листьев")
    assert main_status_total == data["total"], (
        f"main status claims {main_status_total} листьев, GET /dial says total="
        f"{data['total']}: mismatch")


@test("phone_top_default_is_narrow_not_desktop")
def test_phone_top_default_is_narrow_not_desktop(browser: Browser, base_url: str):
    """§2.3: "число идей по умолчанию на узком — 15, не 30" — the ring at 390px is a
    third the desktop's size (the square-by-width fix above), and even 30 no longer
    fits. Checked on a bare `#dial` open with NO `top=` in the address at all, so this is
    the actual default, not an echoed URL param —
    `url_reproduces_the_view_on_a_narrow_phone` already covers "an explicit value
    survives"; this covers "no value at all still lands on the right number"."""
    browser.set_device(390, 844, mobile=True, touch=True)
    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    top_now = read_labeled_number(browser, "идей")
    assert top_now == "15", f"'идей' defaults to {top_now!r} at 390px width, want '15' (§2.3)"


@test("phone_dial_marks_scale_with_ring_width")
def test_phone_dial_marks_scale_with_ring_width(browser: Browser, base_url: str):
    """§2.3: idea-node radius scales off the SVG's OWN width (`wScale(narrow, desktop)`,
    console.html) — 5px at 390px, not the desktop's 3.6px. The constant is not exposed
    directly (it lives inside `drawDial`'s closure as `nodeBaseR`), so it is read back
    the same way the page itself surfaces it: a tapped idea node's own hover ring
    (`.hoverring`, `r = mark.r + 5`, `showMark()`) gives the exact mark radius
    `hoverable()` was called with (`nodeBaseR + 1.5*sqrt(leaves)`), and the card's own
    head text names `leaves` in words ("листьев N") — solving for the one unknown
    (`nodeBaseR`) needs no assumption about which idea got tapped, real data or not."""
    browser.set_device(390, 844, mobile=True, touch=True)
    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    browser.fill("#main textarea", "e2e phone dial-scale probe")
    click_button_with_text(browser, "Разложить")
    browser.wait_for("document.querySelectorAll('.graphwrap svg circle.ideanode').length > 0",
                      timeout=20)

    pt = find_hittable_point(browser, ".graphwrap svg circle.ideanode")
    assert pt, "no idea node reachable by a real tap at 390px"
    browser.tap(pt["x"], pt["y"])
    browser.wait_for(
        "(() => { const r = document.querySelector('.hoverring'); "
        "return r && !r.classList.contains('hide'); })()", timeout=5)
    ring_r = browser.evaluate("Number(document.querySelector('.hoverring').getAttribute('r'))")
    head = browser.evaluate(
        "document.querySelector('.hovercard .hc-head')?.textContent ?? ''")
    m = re.search(r"листьев\s+(\d+)", head)
    assert m, f"tapped idea node's card names no 'листьев N' at all: {head!r}"
    leaves = int(m.group(1))
    mark_r = ring_r - 5   # showMark(): ring.r = best.r + 5
    implied_node_base_r = mark_r - 1.5 * math.sqrt(leaves)
    assert abs(implied_node_base_r - 5) < 1.0, (
        f"implied node base radius {implied_node_base_r:.2f} is not close to the narrow "
        f"target (5px) — from tapped ring r={ring_r}, card's own leaves={leaves}")
    assert abs(implied_node_base_r - 5) < abs(implied_node_base_r - 3.6), (
        f"implied node base radius {implied_node_base_r:.2f} reads closer to the desktop "
        f"constant (3.6) than the narrow one (5) — nodeBaseR looks unscaled at 390px")


@test("phone_dial_ring_gap_scales_with_ring_width")
def test_phone_dial_ring_gap_scales_with_ring_width(browser: Browser, base_url: str):
    """§2.3: the ring's own separation gap (`gapPx`, console.html: `wScale(12, 19)`)
    scales off the SVG's OWN width — 12px at 390px, not the desktop's 19px.

    Not read off the CONVERGED layout: `ringLayout` only enforces `gapPx` as a floor
    (pairs already spread wider are left alone), and on real dial data the outcome is
    either "never binds at all" (few enough ideas that the golden-angle seed already
    clears the floor everywhere — confirmed directly: reading the tightest rendered gap
    at the narrow default's 15 ideas passed unchanged under a broken `gapPx`) or "so
    overcrowded it collapses well past the floor everywhere" (pushed the idea count up
    to force binding — confirmed directly: at 40 and even 300 ideas the tightest gap was
    under 1px, nowhere near either 12 or 19, because co-citation edges pull specific
    pairs together faster than 400 iterations of a half-deficit push can undo on a ring
    this small). Neither regime lets a rendered gap stand in for `gapPx` itself.

    Instead this captures the ARGUMENT `drawDial` actually calls `ringLayout` with,
    exactly once, regardless of what the layout goes on to do with it: `ringLayout` is a
    plain top-level `function` in this classic (non-module) `<script>`, so it is a real
    `window` property, and monkey-patching it before "Разложить" is clicked — call
    through to the original, just also record its own 4th argument — reads the exact
    number the page computed, no convergence or real data involved at all."""
    browser.set_device(390, 844, mobile=True, touch=True)
    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    assert browser.evaluate("typeof window.ringLayout") == "function", (
        "window.ringLayout is not a plain global function any more — this guard's "
        "capture technique (monkey-patching it) no longer applies")
    browser.evaluate("""
      window.__e2e_gapPx = null;
      const orig = window.ringLayout;
      window.ringLayout = function(nodes, links, iters, gapPx) {
        window.__e2e_gapPx = gapPx;
        return orig.apply(this, arguments);
      };
    """)
    browser.fill("#main textarea", "e2e phone dial-scale probe")
    click_button_with_text(browser, "Разложить")
    browser.wait_for("window.__e2e_gapPx !== null", timeout=20)
    gap_px = browser.evaluate("window.__e2e_gapPx")
    assert abs(gap_px - 12) < 0.5, (
        f"drawDial called ringLayout with gapPx={gap_px} at 390px, want close to the "
        f"narrow target (12px)")
    assert abs(gap_px - 12) < abs(gap_px - 19), (
        f"gapPx={gap_px} at 390px reads closer to the desktop constant (19) than the "
        f"narrow one (12) — gapPx looks unscaled at this width")


@test("phone_grow_field_resets_to_natural_height_not_desktop_flex_basis")
def test_phone_grow_field_resets_to_natural_height_not_desktop_flex_basis(browser: Browser, base_url: str):
    """§2.3: `.row{flex-direction:column}` under 620px turns `label.f.grow`'s desktop
    `flex:1 1 340px` (a WIDTH basis in the desktop row) into a HEIGHT basis instead — the
    same 340px, now stretching the hypothesis field itself to 340px tall. The fix resets
    it to `flex:0 0 auto` under that same breakpoint. Checked on the actual rendered
    field, not the stylesheet rule: the label wrapping the dial's own hypothesis textarea
    must stay close to its natural (two-row) height, nowhere near 340px."""
    browser.set_device(390, 844, mobile=True, touch=True)
    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    height = browser.evaluate("""
      (() => {
        const ta = document.querySelector('#main textarea');
        const label = ta.closest('label.f');
        return label ? label.getBoundingClientRect().height : null;
      })()
    """)
    assert height is not None, "no label.f wrapping the dial's own hypothesis textarea"
    assert height < 150, (
        f"hypothesis field (label.f.grow) is {height:.0f}px tall at 390px width — want "
        f"well under the desktop flex-basis (340px); label.f.grow{{flex:0 0 auto}} under "
        f"max-width:620px looks reverted")


@test("phone_stat_strip_edge_chips_reachable_by_scroll")
def test_phone_stat_strip_edge_chips_reachable_by_scroll(browser: Browser, base_url: str):
    """Fail-open hole (previous gate): `phone_no_horizontal_scroll`'s only assertion
    about the header's chip row is `scrollWidth > clientWidth` (`measure_overflow`) —
    true whether the row is `overflow-x:auto` (reachable by scrolling) or
    `overflow-x:hidden` (clipped and gone for good — exactly the "a number is either
    right or explicitly absent" rule this project bans breaking). Measured live at
    390px: the row is ~144px wide and holds ~821px of chips, four of six ("источники …",
    "идеи …", "тезисы …", "рёбра …") entirely past the edge.

    Scrolled with a REAL wheel event (`Browser.mouse_wheel`), not `element.scrollLeft =`
    — Chrome still honours a script write to `scrollLeft` on an `overflow:hidden`
    container (only the user's OWN scroll input is blocked there), so that alone cannot
    tell the two apart. Checks that the scroll actually moved the row AND that the
    "рёбра" chip, once scrolled to, is both unclipped and carries the server's own
    number — a chip that scrolled into view empty or wrong would still fail this."""
    browser.set_device(390, 844)
    browser.goto(f"{base_url}/ui#dial")
    # Not just "any chip" — `refreshStrip()` replaces the WHOLE row in one shot only once
    # both `/healthz` and `/stats` resolve; a transient failure renders a single "bad" chip
    # in the meantime (`stripTimer`'s next tick, 20s later, is the only retry), and that
    # lone chip would satisfy a weaker wait_for without ever carrying "рёбра" at all.
    browser.wait_for(
        "[...document.querySelectorAll('.stat-strip .chip')].some((c) => "
        "c.textContent.includes('рёбра'))", timeout=25)

    with urllib.request.urlopen(f"{base_url}/stats", timeout=10) as r:
        stats = json.load(r)

    geo = browser.evaluate("""
      (() => {
        const strip = document.querySelector('.stat-strip');
        if (!strip) return null;
        const edges = [...strip.querySelectorAll('.chip')]
          .find((c) => c.textContent.includes('рёбра'));
        if (!edges) return null;
        const r = strip.getBoundingClientRect();
        return { x: r.x, y: r.y, width: r.width, height: r.height,
                 scrollWidth: strip.scrollWidth, clientWidth: strip.clientWidth };
      })()
    """)
    assert geo, "no .stat-strip / 'рёбра' chip found at 390px"
    assert geo["scrollWidth"] > geo["clientWidth"] + 1, (
        f".stat-strip does not overflow at 390px (scrollWidth={geo['scrollWidth']}, "
        f"clientWidth={geo['clientWidth']}) — cannot prove anything about reachability "
        f"on this run")

    cx, cy = geo["x"] + geo["width"] / 2, geo["y"] + geo["height"] / 2
    # One comfortably large real scroll, well past the measured ~821px of chips.
    for _ in range(8):
        browser.mouse_wheel(cx, cy, delta_x=200)
        time.sleep(0.05)

    after = browser.evaluate("""
      (() => {
        const strip = document.querySelector('.stat-strip');
        const edges = [...strip.querySelectorAll('.chip')]
          .find((c) => c.textContent.includes('рёбра'));
        const stripRect = strip.getBoundingClientRect();
        const chipRect = edges.getBoundingClientRect();
        return { scrollLeft: strip.scrollLeft, text: edges.textContent,
                 chipLeft: chipRect.left, chipRight: chipRect.right,
                 stripLeft: stripRect.left, stripRight: stripRect.right };
      })()
    """)
    assert after["scrollLeft"] > 0, (
        "a real wheel scroll over .stat-strip did not move it at all — the row is not "
        "actually scrollable (overflow-x is not 'auto')")
    assert (after["chipLeft"] >= after["stripLeft"] - 1
            and after["chipRight"] <= after["stripRight"] + 1), (
        f"'рёбра' chip is still clipped after scrolling the strip as far as it goes: "
        f"chip [{after['chipLeft']:.0f}, {after['chipRight']:.0f}] vs strip "
        f"[{after['stripLeft']:.0f}, {after['stripRight']:.0f}]")

    m = re.search(r"рёбра" + r"\s*([\d" + _THOUSANDS_SEP[1:-1] + r"]+)", after["text"])
    assert m, f"'рёбра' chip has no number in it at all: {after['text']!r}"
    shown_edges = int(re.sub(_THOUSANDS_SEP, "", m.group(1)))
    assert shown_edges == stats["edges"], (
        f"'рёбра' chip (reachable by scroll) shows {shown_edges}, GET /stats says "
        f"edges={stats['edges']}")


# ==================================================== §2.3: touch instead of hover

def dial_svg_view_box(browser: Browser) -> dict:
    """The dial's own `w`/`h` (its `viewBox`, `mk("svg", {viewBox: "0 0 w h"})`) plus the
    SVG element's CURRENT `getBoundingClientRect()` — everything `view_to_screen` below
    needs to invert `toView()` (console.html:1222-1226) and land a dispatched pointer event
    on an exact VIEW-space coordinate rather than a screen pixel guessed by eye. Scrolls the
    SVG into view first (same `scrollIntoView({block:'center', inline:'center'})` as
    `Browser._center_of`/`find_hittable_point` use) — the headless window here is smaller
    than the dial, and a rect read WITHOUT scrolling first is relative to a scroll position
    a caller's dispatched pointer event does not actually land in, off past `window.innerHeight`
    and hitting nothing at all (confirmed directly: `elementFromPoint` at such a point
    returned `null`)."""
    return browser.evaluate("""
      (() => {
        const svg = document.querySelector('.graphwrap svg');
        if (!svg) throw new Error('no .graphwrap svg on screen');
        svg.scrollIntoView({block: 'center', inline: 'center'});
        const vb = svg.viewBox.baseVal;
        const box = svg.getBoundingClientRect();
        return { w: vb.width, h: vb.height,
                 box: { left: box.left, top: box.top, width: box.width, height: box.height } };
      })()
    """)


def view_to_screen(geo: dict, vx: float, vy: float):
    """Exact inverse of `toView()` (console.html:1222-1226) — the SVG is `width:100%` over a
    fixed `viewBox`, letterboxed on whichever axis has slack, so a VIEW-space point (the
    space `marks[]` and `nearestMark()` both work in) is not a screen point until this scale
    and offset are undone. Used instead of eyeballing pixels so a probe point can be placed
    at a computed, provably-correct distance from a mark's own centre (see
    `find_radius_probe_point` below) rather than searched for by trial and error."""
    box, w, h = geo["box"], geo["w"], geo["h"]
    scale = min(box["width"] / w, box["height"] / h)
    sx = box["left"] + (box["width"] - w * scale) / 2 + vx * scale
    sy = box["top"] + (box["height"] - h * scale) / 2 + vy * scale
    return sx, sy


def dial_marks_geometry(browser: Browser) -> dict:
    """Reads back, straight from the rendered SVG, every mark `hoverable()` fed into the
    dial's `marks[]` array (console.html:1060-1063) EXCEPT leaves — callers disable
    "точки-листья" first (`click_checkbox_with_label(browser, "точки-листья")`) so the only
    marks left are idea nodes (rank 1, `svg > g:nth-of-type(4) circle.ideanode` — `gIdeas` is
    the 4th `<g>` `svg.append(gRings, gPts, gEdges, gIdeas, gHits, gLab)` appends, console.html:1214),
    hits (rank 2, r=8 fixed regardless of the dot's own drawn radius — `hoverable(x, y, 8, 2,
    ...)`, console.html:1188 — `gHits` is the 5th `<g>`), and the hypothesis centre (rank 3,
    r=6, the `r="5"` circle appended to `gLab`, the 6th `<g>`). `marks[]` itself lives in a
    closure with no window handle, so this is the only way a test can reconstruct it without
    editing console.html to expose one."""
    return browser.evaluate("""
      (() => {
        const ideas = [...document.querySelectorAll('svg > g:nth-of-type(4) circle.ideanode')]
          .map((c) => ({ cx: +c.getAttribute('cx'), cy: +c.getAttribute('cy'),
                         r: +c.getAttribute('r') }));
        const hits = [...document.querySelectorAll('svg > g:nth-of-type(5) circle')]
          .map((c) => ({ cx: +c.getAttribute('cx'), cy: +c.getAttribute('cy') }));
        const centerCircle = [...document.querySelectorAll('svg > g:nth-of-type(6) circle')]
          .find((c) => c.getAttribute('r') === '5');
        return { ideas, hits,
                 center: centerCircle
                   ? { cx: +centerCircle.getAttribute('cx'), cy: +centerCircle.getAttribute('cy') }
                   : null };
      })()
    """)


def _nearest_mark_score(marks, x, y):
    """Python-side copy of `nearestMark()` (console.html:845-852) — same formula
    (`hypot(dx, dy) - r - rank * 3`), so a probe point's score can be computed and checked
    BEFORE ever touching the browser, rather than hunted for empirically on screen (which
    `verify_five.py`-style scripts in this project's own history warned reads as "almost any
    point is within both radii on 3 040 dense leaves" — the leaf layer is disabled by every
    caller here for exactly that reason)."""
    best, best_score = None, float("inf")
    for m in marks:
        s = math.hypot(m[0] - x, m[1] - y) - m[2] - m[3] * 3
        if s < best_score:
            best_score, best = s, m
    return best, best_score


def find_radius_probe_point(browser: Browser):
    """A VIEW-space point, constructed rather than searched for by luck, that sits strictly
    BETWEEN the two search radii §2.3 specifies (9 px mouse, 18 px touch): exactly
    `idea.r + 15` from some idea node's own centre, along whichever of 24 angles first lands
    with that SAME idea as the nearest mark and a `nearestMark` score of `15 - 1*3 = 12`
    (`9 < 12 < 18`) once EVERY other visible mark (every other idea, every hit, the
    hypothesis centre) is checked too — not assumed clear. Requires "точки-листья" already
    unchecked (dense leaves would put a closer, lower-rank mark within the gap and make no
    such point exist at all — see `dial_marks_geometry`'s docstring). Returns
    `(screen_x, screen_y, idea)` for the caller to hover and then tap at the exact same spot.
    Raises if the current draw genuinely has no such point (extremely unlikely with dozens
    of idea nodes, but a raise here is the honest outcome, not a silent skip)."""
    geo = dial_marks_geometry(browser)
    marks = ([(i["cx"], i["cy"], i["r"], 1) for i in geo["ideas"]]
             + [(h["cx"], h["cy"], 8, 2) for h in geo["hits"]]
             + ([(geo["center"]["cx"], geo["center"]["cy"], 6, 3)] if geo["center"] else []))
    for idea in geo["ideas"]:
        target = (idea["cx"], idea["cy"], idea["r"], 1)
        for deg in range(0, 360, 15):
            theta = math.radians(deg)
            vx = idea["cx"] + (idea["r"] + 15) * math.cos(theta)
            vy = idea["cy"] + (idea["r"] + 15) * math.sin(theta)
            best, score = _nearest_mark_score(marks, vx, vy)
            if best == target and 9 < score < 18:
                sx, sy = view_to_screen(dial_svg_view_box(browser), vx, vy)
                return sx, sy, idea
    raise AssertionError(
        "no VIEW-space point found with a nearestMark score strictly between the mouse (9px) "
        "and touch (18px) search radii — cannot prove the two radii differ on this draw")


def _checkbox_state(browser: Browser, label_text: str, root: str = "#main"):
    """`.checked` of the `<input>` inside the `label.cb` whose own text is exactly
    `label_text`, or `None` if no such checkbox exists — the read-side twin of
    `click_checkbox_with_label`, used so a caller can decide WHETHER to click instead of
    always clicking blind."""
    js = ("(() => { const labels = [...document.querySelectorAll(%s + ' label.cb')]; "
          "const lab = labels.find((l) => l.textContent.trim() === %s); "
          "const input = lab && lab.querySelector('input'); return input ? input.checked : null; })()"
          % (json.dumps(root), json.dumps(label_text)))
    return browser.evaluate(js)


def _draw_dial_for_touch_tests(browser: Browser, base_url: str, hypothesis: str, leaves=False):
    """Shared setup for every test below: a fresh `#dial` load, a hypothesis, "Разложить",
    and "точки-листья" left in whichever state the caller asked for (`leaves`). Idea nodes
    only exist once `withGraph` (checked by default) has actually resolved, hence the wait
    on `circle.ideanode`, not just on the SVG's existence.

    §2.3 makes the checkbox's OWN default width-dependent (unchecked under 430px, checked
    at or above it — see `test_phone_leaves_off_default_status_matches_dial_total`) — so
    this reads `input.checked` first and clicks only when it disagrees with `leaves`,
    rather than clicking unconditionally. A blind click assumed the pre-§2.3 desktop-only
    default (always checked) and silently flipped leaves back ON on a 390px device, which
    is exactly the state every caller here needs OFF (`dial_marks_geometry`'s docstring) —
    confirmed live: that blind click is what made `touch_search_radius_is_wider_than_mouse`
    fail the moment the narrow-width default landed."""
    browser.goto(f"{base_url}/ui#dial")
    browser.wait_for("document.querySelector('#main textarea') !== null", timeout=10)
    browser.fill("#main textarea", hypothesis)
    checked = _checkbox_state(browser, "точки-листья")
    assert checked is not None, "no 'точки-листья' checkbox found on #dial"
    if checked != leaves:
        click_checkbox_with_label(browser, "точки-листья")
    click_button_with_text(browser, "Разложить")
    browser.wait_for("document.querySelectorAll('.graphwrap svg circle.ideanode').length > 0", timeout=20)


def _card_hidden(browser: Browser) -> bool:
    return browser.evaluate(
        "document.querySelector('.hovercard')?.classList.contains('hide') ?? true")


def _card_text(browser: Browser) -> str:
    return browser.evaluate("document.querySelector('.hovercard')?.textContent ?? ''")


@test("mouse_hover_shows_card_and_leave_hides_it")
def test_mouse_hover_shows_card_and_leave_hides_it(browser: Browser, base_url: str):
    """§2.3's mouse half, never actually exercised by an automated test before this one
    (`click_idea_node_opens_it_on_first_load` proves the CLICK path, not hover) — a real
    `pointermove` (`hover_point`, no press) over an idea node must show the `.hovercard` with
    that idea's own text, and moving the mouse back off the SVG entirely must hide it again.
    Checks `event.pointerType` was actually `"mouse"` for the hover itself (installed on the
    page before hovering) so a pass here cannot be an accident of some OTHER input type being
    let through by a broken guard.

    Catches the exact regression `console.html:1282` guards against reverting: swapping the
    listener back from `pointermove` to `mousemove` leaves the real `MouseEvent` with no
    `.pointerType` property at all (`undefined !== "mouse"` in the handler's own guard is
    `true`), so the card never shows — proven by mutation, see this task's write-up."""
    _draw_dial_for_touch_tests(browser, base_url, "e2e mouse hover probe")
    sx, sy, idea = find_radius_probe_point(browser)
    # Actually hover the mark's own centre (inside its radius, score deeply negative — well
    # within 9px), not the constructed probe point above (that one is deliberately just
    # OUTSIDE the mouse radius, see the dedicated radius test below).
    sx_center, sy_center = view_to_screen(dial_svg_view_box(browser), idea["cx"], idea["cy"])

    browser.evaluate("""
      window.__e2e_pointer_types = [];
      document.querySelector('.graphwrap svg').addEventListener('pointermove',
        (e) => window.__e2e_pointer_types.push(e.pointerType));
    """)
    browser.hover_point(sx_center, sy_center)
    browser.wait_for("!(document.querySelector('.hovercard')?.classList.contains('hide') ?? true)",
                      timeout=5)
    assert "идея" in _card_text(browser), (
        f".hovercard is visible but does not read as an idea mark: {_card_text(browser)!r}")
    seen_types = browser.evaluate("window.__e2e_pointer_types")
    assert seen_types and all(t == "mouse" for t in seen_types), (
        f"hover_point's pointermove events were not all pointerType 'mouse': {seen_types!r}")

    browser.hover_point(5, 5)   # top-left corner of the whole page, well outside the svg
    browser.wait_for("document.querySelector('.hovercard')?.classList.contains('hide') === true",
                      timeout=5)


@test("touch_tap_shows_card_via_real_pointer_events")
def test_touch_tap_shows_card_via_real_pointer_events(browser: Browser, base_url: str):
    """§2.3's headline requirement: a real finger tap (not a mouse click wearing a touch
    costume) must show the hovercard, with NO hover involved at all. `Browser.tap()` drives
    `Input.dispatchTouchEvent` directly — confirmed BEFORE writing this test (not assumed) by
    reading `event.pointerType` straight off the page during a real `tap()`, both with and
    without `set_device(touch=True)` active: both read back `"touch"` on `pointerdown` (see
    this task's write-up) — so this asserts the same thing the task demanded proven, on the
    page, every run, not just once by hand.

    Catches: `pointermove`/`pointerdown` reverted to `mousemove`/`mousedown` (a tap never
    fires either — no card, this test times out on the `wait_for` below with a plain,
    readable message, not a silent pass)."""
    browser.set_device(390, 844, mobile=True, touch=True)
    _draw_dial_for_touch_tests(browser, base_url, "e2e touch tap probe")

    browser.evaluate("""
      window.__e2e_pointer_types = [];
      document.querySelector('.graphwrap svg').addEventListener('pointerdown',
        (e) => window.__e2e_pointer_types.push(e.pointerType));
    """)
    pt = find_hittable_point(browser, ".graphwrap svg circle.ideanode")
    assert pt, "no idea node is reachable by a real tap at all"
    browser.tap(pt["x"], pt["y"])

    browser.wait_for("!(document.querySelector('.hovercard')?.classList.contains('hide') ?? true)",
                      timeout=5)
    text = _card_text(browser)
    assert "идея" in text, (
        f"a tap on an idea node did not show the idea's own hovercard text: {text!r}")
    # "идея" + "клик" alone is true of the PRE-§2.3 text too ("...клик — открыть", see
    # 90cdbe3:lake/api/console.html:879) — a straight revert to single-click-opens keeps
    # both substrings and this assertion never notices. Assert the half of the wording that
    # is true on a phone and ABSENT from that old text instead: the old text never mentions
    # a touch screen or a second tap at all, because under it one tap already opened the
    # idea. (If this wording changes again, keep asserting "the head names the touch flow's
    # own two-tap requirement", not this exact string — a check tied to one literal phrase
    # is the same trap this replaces, just with new bait.)
    assert "тач-экране" in text and "второй тап" in text, (
        f"a tap on an idea node's hovercard does not name the touch screen's own two-tap "
        f"requirement: {text!r} — indistinguishable from the pre-§2.3 'клик — открыть' text, "
        f"which also contains 'идея' and 'клик' but describes a single click, not a phone")
    seen_types = browser.evaluate("window.__e2e_pointer_types")
    assert seen_types == ["touch"], (
        f"the tap's own pointerdown was not genuinely pointerType 'touch': {seen_types!r} — "
        f"a test that drives a mouse click and calls it a finger is worse than no test")


@test("touch_second_tap_opens_idea")
def test_touch_second_tap_opens_idea(browser: Browser, base_url: str):
    """§2.3: "тап по метке — показать карточку; тап по той же метке второй раз — открыть
    идею". Two real taps at the SAME screen point on an idea node: the first shows the card
    (checked, so a false pass cannot come from the second tap alone happening to open
    something by coincidence), the second opens the idea via the normal `openIdea()` path —
    reuses `_assert_idea_link_worked` (hash, active tab, rendered heading), the same
    three-part proof every other "does this actually open the idea" test in this file uses.

    Catches: the `best === lastTapMark && best.openId` branch's `openIdea(best.openId)` call
    (console.html:1305) removed or short-circuited — two taps land on the same mark, the card
    keeps showing, `location.hash` never changes, and `_assert_idea_link_worked`'s own
    `wait_for` times out with a readable reason instead of a bare truthiness check."""
    browser.set_device(390, 844, mobile=True, touch=True)
    _draw_dial_for_touch_tests(browser, base_url, "e2e touch second-tap probe")

    pt = find_hittable_point(browser, ".graphwrap svg circle.ideanode")
    assert pt, "no idea node is reachable by a real tap at all"
    browser.tap(pt["x"], pt["y"])
    browser.wait_for("!(document.querySelector('.hovercard')?.classList.contains('hide') ?? true)",
                      timeout=5)
    assert "идея" in _card_text(browser), "first tap did not show the idea's hovercard"

    browser.tap(pt["x"], pt["y"])
    problems = []
    _assert_idea_link_worked(browser, "dial node, second tap", problems)
    assert not problems, "; ".join(problems)


@test("touch_tap_elsewhere_hides_card")
def test_touch_tap_elsewhere_hides_card(browser: Browser, base_url: str):
    """§2.3: "тап мимо — скрыть". A tap on an idea node shows its card; a second tap far from
    every mark (the SVG's own bottom-left corner, well outside the disc's `R + 42`-ish drawn
    radius, checked via `elementFromPoint` landing on the `<svg>` itself and not on some
    decorative element so the tap is known to have actually landed inside the graph) must
    hide it — the same guarantee the mouse side already gets from `pointerleave`, but reached
    here with no `:hover`, no `leave` event at all, just two taps. The BOTTOM edge, not the
    top: at 390px width `header.bar` overlaps the top of the drawn disc (§0.3's own,
    separate, already-known bug — this test does not re-litigate it, just avoids landing a
    tap on the header by accident and mistaking that for proof of anything).

    Catches: `showMark`'s `if (!best || bestScore > radius) { hideCard(); return null; }`
    (console.html:1238) with the `hideCard()` call dropped — the card would stay on screen
    showing the FIRST mark's text after a tap that hit nothing, and this test's own
    `wait_for` on the 'hide' class times out rather than passing on a stale screenshot.

    Also catches the miss branch's OTHER effect, `lastTapMark = null` (console.html:1303),
    dropped on its own: nothing about a stale card would look wrong on screen for that one —
    `hideCard()` alone still runs — so a third tap back on the SAME idea node is required to
    tell. Without the reset, that mark object is still sitting in `lastTapMark` from the
    first tap, so `best === lastTapMark` reads true immediately and the idea opens on this
    ONE tap with no card shown first — exactly the "read the card, then confirm" flow §2.3
    asks for, collapsed back into a single tap. Checked here as "still just a card, hash
    unmoved", the same `_assert_idea_link_worked`-style proof used everywhere else in this
    file that an idea did NOT quietly open."""
    browser.set_device(390, 844, mobile=True, touch=True)
    _draw_dial_for_touch_tests(browser, base_url, "e2e touch tap-elsewhere probe")

    pt = find_hittable_point(browser, ".graphwrap svg circle.ideanode")
    assert pt, "no idea node is reachable by a real tap at all"
    hash_before = browser.evaluate("location.hash")
    browser.tap(pt["x"], pt["y"])
    browser.wait_for("!(document.querySelector('.hovercard')?.classList.contains('hide') ?? true)",
                      timeout=5)

    corner = browser.evaluate("""
      (() => {
        const svg = document.querySelector('.graphwrap svg');
        svg.scrollIntoView({block: 'center', inline: 'center'});
        const box = svg.getBoundingClientRect();
        const x = box.left + 15, y = box.bottom - 15;
        return document.elementFromPoint(x, y) === svg ? { x, y } : null;
      })()
    """)
    assert corner, "the svg's own top-left corner is not hittable — cannot prove 'tap elsewhere'"
    browser.tap(corner["x"], corner["y"])
    browser.wait_for("document.querySelector('.hovercard')?.classList.contains('hide') === true",
                      timeout=5)

    # Re-resolved, not the original `pt`: the corner probe's own `scrollIntoView` just moved
    # the page, so `pt`'s absolute viewport coordinates no longer land on the same idea node
    # — same selector, same (unredrawn) DOM node, freshly re-centred.
    pt2 = find_hittable_point(browser, ".graphwrap svg circle.ideanode")
    assert pt2, "the idea node stopped being tappable after the miss-tap's own scroll"
    browser.tap(pt2["x"], pt2["y"])
    # Waits for EITHER outcome, not just the expected one: if `lastTapMark` survived the
    # miss, this tap opens the idea directly (`hideCard()` inside that branch flips the card
    # back to 'hide' just as fast as a normal show would flip it to visible) — a `wait_for`
    # that only polls for the card becoming visible would then sit until its own timeout and
    # fail with a generic "never became truthy", burying the actual, named assertion below
    # under an unrelated message. Waiting for "card shown OR hash moved" lets whichever one
    # actually happened resolve this quickly, so the hash assertion below is the thing that
    # runs and fails, not a `wait_for` that never gets there.
    browser.wait_for(
        "!(document.querySelector('.hovercard')?.classList.contains('hide') ?? true) || "
        "location.hash !== " + json.dumps(hash_before), timeout=5)
    assert browser.evaluate("location.hash") == hash_before, (
        "a tap on the same idea node, right after a miss, opened the idea on ONE tap — "
        "the miss did not reset lastTapMark, so the mark from before the miss was still "
        "'confirmed' by this tap instead of just showing its card again")


@test("touch_search_radius_is_wider_than_mouse_radius")
def test_touch_search_radius_is_wider_than_mouse(browser: Browser, base_url: str):
    """§2.3: "радиус поиска ближайшей метки: 9 px мышью, 18 px пальцем" — the one claim in
    the spec that is a NUMBER, not a behaviour, and so is the one most easily reverted to a
    single shared constant without anything on screen visibly breaking. Proven with a point,
    constructed (not searched for by hand) to have a `nearestMark` score of exactly 12 —
    strictly between the two radii (`find_radius_probe_point`'s docstring has the geometry):
    a real mouse hover there must NOT show a card (12 > 9), a real tap at such a point must
    show one (12 < 18).

    NOT the same physical point for both halves, on purpose: `set_device(touch=True)` also
    flips `Emulation.setEmitTouchEventsForMouse`, which on this Chromium turns every
    `hover_point()`'s `dispatchMouseEvent` into a synthetic touch sequence instead — the page
    never receives a `pointerType: "mouse"` `pointermove` at all, so `_card_hidden()` would be
    true no matter what the mouse radius actually is (confirmed live: hovering the idea
    node's own centre under that device left the card hidden and the page's own pointermove
    listener recorded nothing). So the mouse half runs on a plain, non-touch viewport, and
    the touch half runs on the real phone viewport, each with its own freshly-drawn probe
    point — two independent constructions of the same "score strictly between 9 and 18"
    geometry, one proven not to show a card on a genuine mouse-only device, the other proven
    to show one on a genuine touch device.

    Catches: the touch handler's own `showMark(vx, vy, 18)` (console.html:1302) reverted to
    `showMark(vx, vy, 9)` — the tap half of this test would then also miss, and the assertion
    names the exact score and both radii rather than just failing blind. Also catches the
    OTHER half of the same guarantee: the `pointermove` listener's own
    `if (event.pointerType !== "mouse") return` (console.html:1292) dropped. A stationary
    `tap()` alone can never reach that — confirmed live: a motionless touch fires
    `pointerdown`/`pointerup` only, no `pointermove` at all — so the tap is followed by one
    real `touchMove` of a couple of px (`tap_and_drag`), the only way to get an actual
    `pointerType: "touch"` `pointermove` onto the page. With the guard in place that event is
    ignored; with it dropped, it falls into the mouse branch and calls `showMark(vx, vy, 9)`
    at a point whose score sits strictly between 9 and 18 — that call finds nothing within
    9px and hides the very card `pointerdown` just showed."""
    browser.set_device(1440, 900, mobile=False, touch=False)
    _draw_dial_for_touch_tests(browser, base_url, "e2e touch radius probe (mouse)")
    sx, sy, idea = find_radius_probe_point(browser)
    browser.hover_point(sx, sy)
    assert _card_hidden(browser), (
        f"a mouse hover at a point with nearestMark score in (9, 18) (idea {idea!r}) showed "
        f"a card — the mouse radius is no longer 9px")

    browser.set_device(390, 844, mobile=True, touch=True)
    _draw_dial_for_touch_tests(browser, base_url, "e2e touch radius probe (touch)")
    sx, sy, idea = find_radius_probe_point(browser)
    browser.tap_and_drag(sx, sy, dx=1, dy=1)
    browser.wait_for("!(document.querySelector('.hovercard')?.classList.contains('hide') ?? true)",
                      timeout=5)
    assert "идея" in _card_text(browser), (
        f"a tap (with a 1px touchmove) at a point with nearestMark score in (9, 18) "
        f"(idea {idea!r}) did not show that idea's own card — either the touch radius is no "
        f"longer wider than the mouse's, or the touch pointermove's own mouse-only guard is "
        f"gone and a nearby mouse-radius(9) miss hid the card `pointerdown` just showed")


@test("hovercard_pinned_to_bottom_below_620px")
def test_hovercard_pinned_to_bottom_below_620px(browser: Browser, base_url: str):
    """§2.3: "при max-width:620 карточка… прижимается к низу экрана на всю ширину". Checked
    both ways — at 390px (phone) the card's rendered geometry must be a fixed, full-width
    strip pinned to the viewport's bottom edge regardless of where the tap landed, and at
    1440px (desktop) the SAME code path (mouse hover, not touch — the desktop case) must
    still place the card NEAR the hovered mark, not pinned — proving the pin is genuinely
    gated on viewport width and not just always-on. `getComputedStyle` is read, not the
    inline `style.left/top` JS still sets (console.html:1276-1277) — the point is that CSS's
    `!important` (console.html:183-187) is what actually wins on screen.

    Catches: the `@media (max-width:620px){ .hovercard{ position:fixed!important; ... } }`
    block deleted — at 390px the card would fall back to `position:absolute` and track the
    tap point instead of pinning to the bottom, and this test's `computed.position` /
    `rect.bottom` assertions name exactly which of those two facts stopped holding."""
    browser.set_device(390, 844, mobile=True, touch=True)
    _draw_dial_for_touch_tests(browser, base_url, "e2e hovercard pin probe")
    pt = find_hittable_point(browser, ".graphwrap svg circle.ideanode")
    assert pt, "no idea node is reachable by a real tap at all"
    browser.tap(pt["x"], pt["y"])
    browser.wait_for("!(document.querySelector('.hovercard')?.classList.contains('hide') ?? true)",
                      timeout=5)

    geo = browser.evaluate("""
      (() => {
        const card = document.querySelector('.hovercard');
        const cs = getComputedStyle(card);
        const r = card.getBoundingClientRect();
        return { position: cs.position, left: r.left, right: r.right, bottom: r.bottom,
                 width: r.width, innerWidth: window.innerWidth, innerHeight: window.innerHeight };
      })()
    """)
    assert geo["position"] == "fixed", (
        f"at 390px width, .hovercard's computed position is {geo['position']!r}, want 'fixed'")
    assert abs(geo["left"]) < 1 and abs(geo["width"] - geo["innerWidth"]) < 1, (
        f".hovercard at 390px is not full-width: left={geo['left']}, width={geo['width']}, "
        f"innerWidth={geo['innerWidth']}")
    assert abs(geo["bottom"] - geo["innerHeight"]) < 1, (
        f".hovercard at 390px is not pinned to the bottom edge: bottom={geo['bottom']}, "
        f"innerHeight={geo['innerHeight']}")

    browser.clear_device()
    browser.set_device(1440, 900, mobile=False, touch=False)
    _draw_dial_for_touch_tests(browser, base_url, "e2e hovercard pin probe wide")
    idea = dial_marks_geometry(browser)["ideas"][0]
    sx_center, sy_center = view_to_screen(dial_svg_view_box(browser), idea["cx"], idea["cy"])
    browser.hover_point(sx_center, sy_center)
    browser.wait_for("!(document.querySelector('.hovercard')?.classList.contains('hide') ?? true)",
                      timeout=5)
    wide_geo = browser.evaluate("""
      (() => { const card = document.querySelector('.hovercard');
                const cs = getComputedStyle(card);
                const r = card.getBoundingClientRect();
                return { position: cs.position, bottom: r.bottom, width: r.width,
                         innerHeight: window.innerHeight }; })()
    """)
    assert wide_geo["position"] != "fixed", (
        f"at 1440px width, .hovercard's computed position is {wide_geo['position']!r} — the "
        f"phone-only pin is leaking into desktop")
    assert wide_geo["width"] < 400, (
        f".hovercard at 1440px is {wide_geo['width']}px wide — looks like the full-width "
        f"phone strip, not the small card next to the pointer")


@test("touch_screens_never_match_hover_hover")
def test_touch_screens_never_match_hover_hover(browser: Browser, base_url: str):
    """§2.3: "`@media (hover:hover)` — снять `:hover`-эффекты, которые на тач-экране
    залипают (`table.tbl tr:hover`, `circle.ideanode:hover`)". `window.matchMedia` is the
    same signal those two CSS rules (console.html:101, :170) are gated on, read directly from
    the page instead of re-deriving it from screenshots — a touch/mobile-emulated viewport
    must report `hover: none` / `pointer: coarse`, and a real desktop viewport must report
    `hover: hover`, so the CSS actually stops applying on one and keeps applying on the
    other rather than being gated on something that never differs in this environment.

    Not a mutation-guarded test (the CSS gate itself is Chrome's own media-feature
    evaluation, not application logic this suite can break with an edit) — it exists to
    catch the two `@media (hover:hover)` blocks being removed entirely, at which point
    `table.tbl tr:hover td` / `circle.ideanode:hover` apply unconditionally again and this
    test's own read of the STYLESHEET (not just the media feature) below would stop finding
    them scoped."""
    browser.set_device(390, 844, mobile=True, touch=True)
    browser.goto(f"{base_url}/ui#theses")
    browser.wait_for("document.querySelector('#main table.tbl') !== null", timeout=10)
    touch_hover = browser.evaluate("window.matchMedia('(hover: hover)').matches")
    assert touch_hover is False, (
        f"matchMedia('(hover: hover)') is {touch_hover!r} on a touch/mobile-emulated "
        f"viewport, want False — table/idea-node hover CSS would apply unconditionally")

    browser.clear_device()
    browser.goto(f"{base_url}/ui#theses")
    browser.wait_for("document.querySelector('#main table.tbl') !== null", timeout=10)
    mouse_hover = browser.evaluate("window.matchMedia('(hover: hover)').matches")
    assert mouse_hover is True, (
        f"matchMedia('(hover: hover)') is {mouse_hover!r} on a plain desktop viewport, want "
        f"True — this environment cannot tell hover-gated CSS apart from unconditional CSS "
        f"if this ever goes False here too")

    gated = browser.evaluate("""
      (() => {
        let sawTr = false, sawIdeanode = false;
        for (const sheet of document.styleSheets) {
          for (const rule of sheet.cssRules) {
            if (rule instanceof CSSMediaRule && /hover\\s*:\\s*hover/.test(rule.conditionText || rule.media.mediaText)) {
              const text = [...rule.cssRules].map((r) => r.selectorText).join(' ');
              if (text.includes('tr:hover')) sawTr = true;
              if (text.includes('ideanode:hover')) sawIdeanode = true;
            }
          }
        }
        return { sawTr, sawIdeanode };
      })()
    """)
    assert gated["sawTr"], (
        "no @media(hover:hover) block contains a 'tr:hover' rule — table.tbl tr:hover is not "
        "gated, and would stick on a touch tap with no pointer left to leave")
    assert gated["sawIdeanode"], (
        "no @media(hover:hover) block contains an 'ideanode:hover' rule — "
        "circle.ideanode:hover is not gated, and would stick on a touch tap with no pointer "
        "left to leave")


@test("mouse_miss_inside_svg_hides_card")
def test_mouse_miss_inside_svg_hides_card(browser: Browser, base_url: str):
    """§2.3's mouse-miss branch, proven with the pointer still INSIDE the svg's own bounding
    box — `mouse_hover_shows_card_and_leave_hides_it`, above, only ever leaves the card by
    moving the mouse to (5, 5), well outside the svg entirely, which hides the card through
    the completely separate `pointerleave` listener and never touches `showMark`'s own
    `if (!best || bestScore > radius) { hideCard(); ... }` branch at all. This hovers the
    svg's own bottom-left corner instead — `elementFromPoint` confirmed to land on the
    `<svg>` itself, the exact corner `touch_tap_elsewhere_hides_card` already trusts for the
    touch side of the same claim — reached via `pointermove`, with no `pointerleave` in
    between, so a card left over from an earlier hover can only have gone away through
    `showMark`'s own miss branch.

    Catches: `hideCard()` inside `showMark`'s miss branch gated to only the touch/pen
    `pointerdown` caller (e.g. behind `radius === 18`) — the mouse's own miss (radius 9)
    would then leave the previous mark's card on screen, reading as if the pointer still
    sat over that mark while it visibly does not."""
    browser.set_device(1440, 900, mobile=False, touch=False)
    _draw_dial_for_touch_tests(browser, base_url, "e2e mouse miss-inside-svg probe")
    geo = dial_marks_geometry(browser)
    # A hit that lands "on its own idea" is drawn at that idea's EXACT (cx, cy) — same
    # point, but rank 2 (hit) beats rank 1 (idea) in `nearestMark`'s score, so hovering
    # `ideas[0]` (the highest-cosine idea, i.e. the one most likely to also be a top hit)
    # would show the HIT's card instead and this assertion would never even get to run.
    # Picking any idea whose position no hit shares sidesteps that entirely — which mark
    # this shows is not what this test is about, only that hovering IT shows a card.
    hit_positions = {(round(h["cx"], 1), round(h["cy"], 1)) for h in geo["hits"]}
    idea = next((i for i in geo["ideas"]
                 if (round(i["cx"], 1), round(i["cy"], 1)) not in hit_positions), None)
    assert idea, "every visible idea node coincides with a hit mark — cannot isolate a plain hover"
    sx, sy = view_to_screen(dial_svg_view_box(browser), idea["cx"], idea["cy"])
    browser.hover_point(sx, sy)
    browser.wait_for("!(document.querySelector('.hovercard')?.classList.contains('hide') ?? true)",
                      timeout=5)

    corner = browser.evaluate("""
      (() => {
        const svg = document.querySelector('.graphwrap svg');
        svg.scrollIntoView({block: 'center', inline: 'center'});
        const box = svg.getBoundingClientRect();
        const x = box.left + 15, y = box.bottom - 15;
        return document.elementFromPoint(x, y) === svg ? { x, y } : null;
      })()
    """)
    assert corner, "the svg's own bottom-left corner is not hittable — cannot prove 'miss inside svg'"
    browser.hover_point(corner["x"], corner["y"])
    browser.wait_for("document.querySelector('.hovercard')?.classList.contains('hide') === true",
                      timeout=5)


@test("hit_mark_second_tap_does_not_open_idea")
def test_hit_mark_second_tap_does_not_open_idea(browser: Browser, base_url: str):
    """`openId` is set ONLY for idea nodes (console.html's own comment on `hoverable`, right
    above where `marks` is built) — a hit mark (the numbered dots on the spokes to the
    centre) is handed none, so two real taps on the SAME hit mark must just keep showing its
    card, never move `location.hash`. Proven against the hit marks' own geometry
    (`svg > g:nth-of-type(5) circle`, the same selector `dial_marks_geometry`'s `hits` reads
    — `gHits` is the 5th `<g>` `drawDial` appends), not an idea node, so a guard that only
    checks 'is this the idea `<circle>`'s own click listener' rather than 'does this
    PARTICULAR mark carry an `openId`' cannot pass by accident.

    Catches: the hit marks' own `hoverable(x, y, 8, 2, head, hit.text)` call (console.html,
    inside `data.hits.forEach`) handed a fifth argument it was never meant to carry (e.g.
    `hit.idea_id`) — a second tap on the mark would then satisfy
    `best === lastTapMark && best.openId` same as an idea node, and `location.hash` would
    move to `#ideas/...` for a mark nothing on screen ever named as an idea."""
    browser.set_device(390, 844, mobile=True, touch=True)
    _draw_dial_for_touch_tests(browser, base_url, "e2e hit mark second-tap probe")

    pt = find_hittable_point(browser, "svg > g:nth-of-type(5) circle")
    assert pt, "no hit mark is reachable by a real tap at all"
    hash_before = browser.evaluate("location.hash")

    browser.tap(pt["x"], pt["y"])
    browser.wait_for("!(document.querySelector('.hovercard')?.classList.contains('hide') ?? true)",
                      timeout=5)
    assert "cos" in _card_text(browser), "first tap on a hit mark did not show its own hovercard"

    browser.tap(pt["x"], pt["y"])
    assert browser.evaluate("location.hash") == hash_before, (
        "a second tap on a HIT mark (not an idea node) moved location.hash — hit marks carry "
        "no openId, and a second tap on one must never call openIdea()")
    assert not _card_hidden(browser), (
        "a second tap on a hit mark hid its card instead of just showing it again — a mark "
        "with no openId should behave exactly like the first tap, every time")


@test("pen_second_tap_opens_idea")
def test_pen_second_tap_opens_idea(browser: Browser, base_url: str):
    """§2.3's two-tap flow gates on `hasHover` (console.html) being false, not on
    `pointerType === "touch"` specifically — a real stylus tap reports `pointerType: "pen"`
    on both its `pointerdown` and its own synthetic `click` (Chromium; confirmed below by
    reading `event.pointerType` back off the page, not assumed), which is exactly why the
    click guard used to read `if (event.pointerType === "touch") return;` and a pen's click
    (`!== "touch"`) sailed straight through the two-tap flow that guard was meant to gate.
    Drives a REAL pen via `Browser.pen_tap` (`Input.dispatchMouseEvent` with
    `pointerType: "pen"`), not a mouse click wearing a pen label.

    Catches: `hasHover`'s single `=== "mouse"` comparison reverted back to a per-listener
    `!== "touch"` (or `=== "touch"`) pair — a pen's click passes THAT condition, so the idea
    opens on the FIRST tap with no card shown first, and `location.hash` moves before this
    test's own first assertion ever runs."""
    _draw_dial_for_touch_tests(browser, base_url, "e2e pen second-tap probe")

    pt = find_hittable_point(browser, ".graphwrap svg circle.ideanode")
    assert pt, "no idea node is reachable by a real tap at all"
    hash_before = browser.evaluate("location.hash")

    browser.evaluate("""
      window.__e2e_pen_types = [];
      document.querySelector('.graphwrap svg').addEventListener('pointerdown',
        (e) => window.__e2e_pen_types.push(e.pointerType));
    """)
    browser.pen_tap(pt["x"], pt["y"])
    browser.wait_for("!(document.querySelector('.hovercard')?.classList.contains('hide') ?? true)",
                      timeout=5)
    assert "идея" in _card_text(browser), "first pen tap did not show the idea's own hovercard"
    assert browser.evaluate("location.hash") == hash_before, (
        "a SINGLE pen tap on an idea node already moved location.hash — the pen's synthetic "
        "click is bypassing the two-tap flow (hasHover must treat 'pen' the same as 'touch')")
    seen_types = browser.evaluate("window.__e2e_pen_types")
    assert seen_types == ["pen"], (
        f"pen_tap's own pointerdown was not genuinely pointerType 'pen': {seen_types!r} — "
        f"a test that drives a mouse and calls it a pen is worse than no test")

    browser.pen_tap(pt["x"], pt["y"])
    problems = []
    _assert_idea_link_worked(browser, "dial node, second pen tap", problems)
    assert not problems, "; ".join(problems)


@test("pen_tap_after_other_hide_does_not_open")
def test_pen_tap_after_other_hide_does_not_open(browser: Browser, base_url: str):
    """The other half of the same guard: once the card has been hidden by a COMPLETELY
    different pointer path than the miss-tap `touch_tap_elsewhere_hides_card` already covers
    — here, a genuine mouse hovering away, which hides the card through the `pointerleave`
    listener, an input type the pen tap that armed the mark never touches — a further SINGLE
    pen tap on the SAME idea node must only show its card again, not open it. Proves
    `lastTapMark` is disarmed by `hideCard()` ITSELF, for every caller that can hide the
    card, not merely the one path (`showMark`'s own miss branch) the existing touch test
    happens to exercise.

    Catches: `lastTapMark = null` living only inside the specific branch that used to call
    it (e.g. reverted to sit solely in `showMark`'s miss case, or in the `pointerdown` open
    branch, rather than inside `hideCard()` itself, which every hide path already calls) —
    with the reset scoped that narrowly, a mark armed by a pen tap and then hidden by an
    unrelated mouse `pointerleave` would still be sitting in `lastTapMark`, so this second
    pen tap would open the idea on ONE tap instead of just showing its card."""
    browser.set_device(1440, 900, mobile=False, touch=False)
    _draw_dial_for_touch_tests(browser, base_url, "e2e pen cross-input disarm probe")

    pt = find_hittable_point(browser, ".graphwrap svg circle.ideanode")
    assert pt, "no idea node is reachable by a real tap at all"
    hash_before = browser.evaluate("location.hash")

    browser.pen_tap(pt["x"], pt["y"])
    browser.wait_for("!(document.querySelector('.hovercard')?.classList.contains('hide') ?? true)",
                      timeout=5)
    assert "идея" in _card_text(browser), "the arming pen tap did not show the idea's card"

    browser.hover_point(5, 5)   # a real mouse, well outside the svg — pointerleave, not a miss-tap
    browser.wait_for("document.querySelector('.hovercard')?.classList.contains('hide') === true",
                      timeout=5)

    browser.pen_tap(pt["x"], pt["y"])
    browser.wait_for(
        "!(document.querySelector('.hovercard')?.classList.contains('hide') ?? true) || "
        "location.hash !== " + json.dumps(hash_before), timeout=5)
    assert browser.evaluate("location.hash") == hash_before, (
        "a pen tap on an idea node, right after the card was hidden by an UNRELATED mouse "
        "pointerleave, opened the idea on ONE tap — lastTapMark survived a hide it was not "
        "reset by, so a stale mark from before that hide was still 'confirmed' by this tap")


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

