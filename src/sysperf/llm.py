"""
Runtime LLM provider abstraction for SysPerf's self-evolution subsystem.

Nothing in the monitoring path depends on this module. If no provider is
configured, the network is down or the credentials are wrong, every call
returns an ``LLMError`` and the caller degrades to rule-based behaviour.

Providers
---------
* ``anthropic`` — the Anthropic Messages API over HTTPS (stdlib only, so no new
  runtime dependency). The model id is never hard-coded: it comes from settings
  and is validated against ``GET /v1/models`` (``list_models``).
* ``kiro_cli`` — a locally installed ``kiro-cli`` in headless mode with **no
  tools trusted**. Useful where an organisation provides Claude through Kiro
  rather than a direct API key.

Security
--------
* Keys come from ``SYSPERF_ANTHROPIC_API_KEY`` / ``ANTHROPIC_API_KEY`` or a key
  file that must be private (0600 on POSIX). Keys are never logged, never
  returned by any API, and are scrubbed from every error string.
* Model output is untrusted: ``complete_json`` extracts and validates a JSON
  object against a small schema before anything else sees it.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .background_process import run_hidden

log = logging.getLogger("sysperf.llm")
# Read through this name (not os.name) so tests can simulate the other OS
# without patching the global os module, which breaks pathlib on Windows.
OS_NAME = os.name

ANTHROPIC_VERSION = "2023-06-01"          # API version header, not a model id
DEFAULT_BASE_URL = "https://api.anthropic.com"
_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class LLMError(Exception):
    """Any provider failure. ``kind`` is machine-readable for the dashboard."""

    def __init__(self, kind: str, message: str, retryable: bool = False):
        super().__init__(redact(message))
        self.kind = kind
        self.retryable = retryable


def redact(text: Any, secrets: tuple = ()) -> str:
    """Remove anything that looks like an API key from ``text``."""
    out = _KEY_PATTERN.sub("sk-ant-***", str(text))
    for s in secrets:
        if s and len(s) >= 8:
            out = out.replace(s, "***")
    return out


class RedactingFilter(logging.Filter):
    """Logging filter: no record may carry an API key, whatever logged it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:            # pragma: no cover - malformed record
            return True
        clean = redact(msg)
        if clean != msg:
            record.msg, record.args = clean, ()
        return True


# --------------------------------------------------------------- credentials --
# On Windows chmod cannot make a file private, so the key file holds a DPAPI
# blob instead: only the same Windows user on the same PC can decrypt it.
DPAPI_PREFIX = "dpapi:"


class _WinDpapi:                     # pragma: no cover - Windows only (test_llm_win.py)
    """CryptProtectData / CryptUnprotectData for the current user."""

    UI_FORBIDDEN = 0x1

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        self._c, self._Blob = ctypes, Blob
        # A private WinDLL, so these argtypes cannot clash with another module's
        # (the 4.1.0 tray-quit crash came from shared ctypes function objects).
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel = ctypes.WinDLL("kernel32")
        self._kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel.LocalFree.restype = ctypes.c_void_p
        p_blob = ctypes.POINTER(Blob)
        args = [p_blob, ctypes.c_void_p, p_blob, ctypes.c_void_p, ctypes.c_void_p,
                wintypes.DWORD, p_blob]
        self._protect, self._unprotect = crypt.CryptProtectData, crypt.CryptUnprotectData
        for fn in (self._protect, self._unprotect):
            fn.argtypes, fn.restype = args, wintypes.BOOL

    def _call(self, fn, data: bytes) -> bytes:
        c = self._c
        buf = c.create_string_buffer(data, len(data))
        inp = self._Blob(len(data), c.cast(buf, c.POINTER(c.c_char)))
        out = self._Blob()
        if not fn(c.byref(inp), None, None, None, None, self.UI_FORBIDDEN, c.byref(out)):
            raise OSError(c.get_last_error(), "DPAPI call failed")
        try:
            return c.string_at(out.pbData, out.cbData)
        finally:
            self._kernel.LocalFree(c.cast(out.pbData, c.c_void_p))

    def protect(self, data: bytes) -> bytes:
        return self._call(self._protect, data)

    def unprotect(self, blob: bytes) -> bytes:
        return self._call(self._unprotect, blob)


_DPAPI_INSTANCE: list = []


def _dpapi():
    """The DPAPI wrapper on Windows (built once), else None. Tests replace this."""
    if OS_NAME != "nt":
        return None
    if not _DPAPI_INSTANCE:                        # pragma: no cover - Windows only
        _DPAPI_INSTANCE.append(_WinDpapi())
    return _DPAPI_INSTANCE[0]                      # pragma: no cover - Windows only


def _read_key_file(p: Path) -> tuple:
    """(key or None, source) from a key file, decrypting a DPAPI blob."""
    text = p.read_text(encoding="utf-8").strip()
    if not text.startswith(DPAPI_PREFIX):
        return (text or None), "file"
    dp = _dpapi()
    if dp is None:
        log.warning("ignoring %s: it is DPAPI-encrypted and this is not Windows", p.name)
        return None, "file:unreadable"
    try:
        key = dp.unprotect(base64.b64decode(text[len(DPAPI_PREFIX):])).decode("utf-8")
    except (OSError, ValueError) as exc:
        log.warning("ignoring %s: cannot decrypt it as this user (%s)", p.name, exc)
        return None, "file:unreadable"
    return (key.strip() or None), "file:dpapi"


def load_api_key(key_file: Path | None, env=None) -> tuple:
    """(key or None, source). Refuses a key file other users can read.

    A plaintext key file on Windows (written before 5.0.0) is encrypted in
    place the first time it is read.
    """
    env = os.environ if env is None else env
    for var in ("SYSPERF_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"):
        if env.get(var):
            return env[var].strip(), f"env:{var}"
    if key_file and Path(key_file).is_file():
        p = Path(key_file)
        if OS_NAME != "nt" and (p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)):   # pragma: posix only
            log.warning("ignoring %s: readable by other users (chmod 600 it)", p.name)
            return None, "file:insecure-permissions"
        key, source = _read_key_file(p)
        if key and source == "file" and _dpapi() is not None:
            try:
                store_api_key(p, key)
                source = "file:dpapi"
                log.info("encrypted %s with DPAPI", p.name)
            except OSError as exc:
                log.warning("could not encrypt %s: %s", p.name, exc)
        return key, source
    return None, "none"


def store_api_key(key_file: Path, key: str) -> None:
    """Write the key privately: DPAPI on Windows, else mode 0600 (atomic, never logged)."""
    key = (key or "").strip()
    if not key or len(key) > 512 or any(c.isspace() for c in key):
        raise ValueError("API key must be a single non-empty token")
    dp = _dpapi()
    payload = (DPAPI_PREFIX + base64.b64encode(dp.protect(key.encode("utf-8"))).decode("ascii")
               if dp is not None else key)
    key_file = Path(key_file)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(key_file.parent, 0o700)
    except OSError:                  # pragma: no cover - e.g. Windows
        pass
    tmp = key_file.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
    os.replace(tmp, key_file)


# ------------------------------------------------------------------- results --
@dataclass
class Completion:
    text: str
    model: str
    provider: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_s: float = 0.0
    attempts: int = 1


@dataclass
class UsageMeter:
    """Thread-safe daily token / cost accounting with a hard budget."""
    daily_token_budget: int = 200_000
    daily_cost_budget_usd: float = 2.0
    _day: str = ""
    tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _roll(self, clock=time.time) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(clock()))
        if day != self._day:
            self._day, self.tokens, self.cost_usd, self.calls = day, 0, 0.0, 0

    def check(self) -> str | None:
        with self._lock:
            self._roll()
            if self.daily_token_budget and self.tokens >= self.daily_token_budget:
                return f"daily token budget exhausted ({self.tokens}/{self.daily_token_budget})"
            if self.daily_cost_budget_usd and self.cost_usd >= self.daily_cost_budget_usd:
                return (f"daily cost budget exhausted (${self.cost_usd:.2f}/"
                        f"${self.daily_cost_budget_usd:.2f})")
            return None

    def add(self, c: Completion) -> None:
        with self._lock:
            self._roll()
            self.calls += 1
            self.tokens += (c.input_tokens or 0) + (c.output_tokens or 0)
            self.cost_usd += c.cost_usd or 0.0

    def snapshot(self) -> dict:
        with self._lock:
            self._roll()
            return {"day": self._day, "calls": self.calls, "tokens": self.tokens,
                    "cost_usd": round(self.cost_usd, 4),
                    "daily_token_budget": self.daily_token_budget,
                    "daily_cost_budget_usd": self.daily_cost_budget_usd}


def estimate_cost(prices: dict, model: str, tin: int | None,
                  tout: int | None) -> float | None:
    """USD from a user-supplied price table {model: [in_per_Mtok, out_per_Mtok]}.

    Prices are configuration, not code: they change, and a wrong hard-coded
    number would make the budget lie. Unknown price -> None (shown as unknown).
    """
    row = (prices or {}).get(model)
    if not row or tin is None or tout is None:
        return None
    return (tin * float(row[0]) + tout * float(row[1])) / 1_000_000


# ----------------------------------------------------------------- providers --
class Provider:
    name = "none"

    def complete(self, system: str, prompt: str, max_tokens: int) -> Completion:
        raise LLMError("not_configured", "no LLM provider is configured")

    def list_models(self) -> list:
        return []


# Auto model selection (model "auto" or blank): the newest model of the first
# family present in GET /v1/models, which lists newest first. Re-resolved daily
# and immediately when the chosen id is retired (HTTP 404).
AUTO_MODEL_PREFERENCE = ("opus", "sonnet")
AUTO_MODEL_TTL_S = 24 * 3600


def pick_auto_model(ids: list) -> str | None:
    for family in AUTO_MODEL_PREFERENCE:
        for mid in ids:                       # API order: newest first
            if family in mid.lower():
                return mid
    return ids[0] if ids else None


class AnthropicProvider(Provider):
    """Anthropic Messages API with timeout, retries, backoff and 429 handling."""
    name = "anthropic"

    def __init__(self, api_key: str | None, model: str | None,
                 base_url: str = DEFAULT_BASE_URL, timeout_s: float = 60.0,
                 max_retries: int = 4, backoff_base_s: float = 1.0,
                 prices: dict | None = None, opener=None, sleep=time.sleep):
        self.api_key = api_key
        # Only an explicit "auto" selects automatically (build_provider defaults
        # the setting to it); None keeps the explicit "no model configured" error.
        self.auto = (model or "").strip().lower() == "auto"
        self.model = None if self.auto else model
        self._auto_at = 0.0
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.prices = prices or {}
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple:
        if not self.api_key:
            raise LLMError("no_credentials", "no Anthropic API key configured")
        data = json.dumps(body).encode() if body is not None else None
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(
                self.base_url + path, data=data, method=method,
                headers={"x-api-key": self.api_key,
                         "anthropic-version": ANTHROPIC_VERSION,
                         "content-type": "application/json"})
            try:
                with self._open(req, timeout=self.timeout_s) as resp:
                    return json.loads(resp.read().decode("utf-8")), attempt
            except urllib.error.HTTPError as exc:
                code = exc.code
                try:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                except Exception:    # pragma: no cover
                    detail = ""
                if code in (401, 403):
                    raise LLMError("auth", f"credentials rejected (HTTP {code})") from exc
                if code == 404:
                    raise LLMError("not_found", f"HTTP 404 — unknown model or endpoint: {detail}") from exc
                if code in (400, 413, 422):
                    raise LLMError("bad_request", f"HTTP {code}: {detail}") from exc
                retryable = code == 429 or code >= 500
                kind = "rate_limited" if code == 429 else "server_error"
                if not retryable or attempt > self.max_retries:
                    raise LLMError(kind, f"HTTP {code} after {attempt} attempt(s): {detail}",
                                   retryable=retryable) from exc
                wait = self._retry_after(exc) or self._backoff(attempt)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt > self.max_retries:
                    raise LLMError("network", f"unreachable after {attempt} attempt(s): "
                                   f"{getattr(exc, 'reason', exc)}", retryable=True) from exc
                wait = self._backoff(attempt)
            except ValueError as exc:
                raise LLMError("bad_response", f"non-JSON response: {exc}") from exc
            log.info("anthropic: retrying in %.1fs (attempt %d)", wait, attempt)
            self._sleep(wait)

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped at 30 s."""
        return min(30.0, self.backoff_base_s * (2 ** (attempt - 1))) * (0.5 + random.random() / 2)

    @staticmethod
    def _retry_after(exc) -> float | None:
        try:
            v = exc.headers.get("retry-after")
            return min(60.0, float(v)) if v else None
        except (TypeError, ValueError, AttributeError):
            return None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _resolve_model(self, force: bool = False) -> None:
        if not self.auto:
            return
        if force or not self.model or time.time() - self._auto_at > AUTO_MODEL_TTL_S:
            chosen = pick_auto_model(self.list_models())
            if not chosen:
                raise LLMError("not_configured", "auto model selection: the provider "
                               "listed no models for this key")
            if chosen != self.model:
                log.info("anthropic: auto-selected model %s", chosen)
            self.model, self._auto_at = chosen, time.time()

    def complete(self, system: str, prompt: str, max_tokens: int) -> Completion:
        self._resolve_model()
        if not self.model:
            raise LLMError("not_configured", "no model configured (choose one from the "
                           "provider's model list, or 'auto')")
        started = time.time()
        req = {"model": self.model, "max_tokens": int(max_tokens), "system": system,
               "messages": [{"role": "user", "content": prompt}]}
        try:
            body, attempts = self._request("POST", "/v1/messages", req)
        except LLMError as exc:
            if not (self.auto and exc.kind == "not_found"):
                raise
            self._resolve_model(force=True)          # chosen model was retired
            req["model"] = self.model
            body, attempts = self._request("POST", "/v1/messages", req)
        try:
            text = "".join(b.get("text", "") for b in body.get("content", [])
                           if b.get("type") == "text")
            usage = body.get("usage") or {}
            tin, tout = usage.get("input_tokens"), usage.get("output_tokens")
        except (AttributeError, TypeError) as exc:
            raise LLMError("bad_response", f"unexpected response shape: {exc}") from exc
        return Completion(text=text, model=body.get("model", self.model),
                          provider=self.name, input_tokens=tin, output_tokens=tout,
                          cost_usd=estimate_cost(self.prices, self.model, tin, tout),
                          latency_s=time.time() - started, attempts=attempts)

    def list_models(self) -> list:
        body, _ = self._request("GET", "/v1/models?limit=100")
        return [m.get("id") for m in body.get("data", []) if m.get("id")]


class KiroCliProvider(Provider):
    """Headless ``kiro-cli`` with no tools trusted; output is plain text."""
    name = "kiro_cli"

    def __init__(self, model: str | None = None, executable: str = "kiro-cli",
                 timeout_s: float = 300.0, run=run_hidden):
        self.model = model
        self.executable = executable
        self.timeout_s = timeout_s
        self._run = run

    def complete(self, system: str, prompt: str, max_tokens: int) -> Completion:
        argv = [self.executable, "chat", "--no-interactive", "--trust-tools="]
        if self.model:
            argv += ["--model", self.model]
        argv.append(f"{system}\n\n{prompt}")
        started = time.time()
        try:
            res = self._run(argv, capture_output=True, text=True, timeout=self.timeout_s,
                            stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise LLMError("not_configured", f"{self.executable} not found") from None
        except subprocess.TimeoutExpired:
            raise LLMError("timeout", f"kiro-cli timed out after {self.timeout_s}s",
                           retryable=True) from None
        out = _ANSI.sub("", res.stdout or "")
        if res.returncode != 0 or not out.strip():
            raise LLMError("provider_error", f"kiro-cli rc={res.returncode}: "
                           f"{_ANSI.sub('', res.stderr or '')[-300:]}", retryable=True)
        out = decode_kiro_stream(out)
        return Completion(text=out, model=self.model or "kiro-default",
                          provider=self.name, latency_s=time.time() - started)


class ClaudeCodeProvider(Provider):
    """Headless Claude Code (``claude -p``) using the signed-in account.

    All tools are disabled (``--tools ""``) so the model can only return text —
    it cannot read or write files or run commands; the evolution engine applies
    any patch itself, in an isolated worktree. No session is persisted. Model
    ``auto`` (or blank) passes no ``--model``, so Claude Code uses the account's
    default model; any alias it accepts ('opus', 'sonnet', ...) or a full model
    id may be set instead.
    """
    name = "claude_code"
    WINGET_LINK = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\claude.exe")

    # The signed-in copy may live in WSL (~/.local/bin/claude) rather than on
    # Windows. --tools "" sits inside the script so the empty argument cannot be
    # lost crossing the wsl.exe boundary.
    WSL_PREFIX = ["wsl.exe", "-e", "bash", "-lc",
                  'exec "$HOME/.local/bin/claude" -p --tools "" "$@"', "claude"]

    def __init__(self, model: str | None = None, executable: str | None = None,
                 timeout_s: float = 600.0, run=run_hidden, via: str = "auto"):
        m = (model or "auto").strip()
        self.model = None if m.lower() == "auto" else m
        self._run = run
        self.via_wsl = via == "wsl" or (via == "auto" and OS_NAME == "nt"
                                         and executable is None and self._wsl_has_claude())
        self.executable = "wsl.exe" if self.via_wsl else (executable or self._find())
        self.timeout_s = timeout_s

    def _wsl_has_claude(self) -> bool:
        try:
            r = self._run(["wsl.exe", "-e", "sh", "-c", 'test -x "$HOME/.local/bin/claude"'],
                          capture_output=True, timeout=30)
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    @classmethod
    def _find(cls) -> str:
        import shutil
        found = shutil.which("claude")
        if found:
            return found
        return cls.WINGET_LINK if os.path.exists(cls.WINGET_LINK) else "claude"

    @property
    def configured(self) -> bool:
        import shutil
        return os.path.exists(self.executable) or bool(shutil.which(self.executable))

    def list_models(self) -> list:
        # Claude Code resolves these aliases to the latest model of each family.
        return ["auto", "opus", "sonnet", "fable", "haiku"]

    def complete(self, system: str, prompt: str, max_tokens: int) -> Completion:
        base = (list(self.WSL_PREFIX) if self.via_wsl
                else [self.executable, "-p", "--tools", ""])
        argv = [*base, "--output-format", "json", "--no-session-persistence", "--system-prompt", system]
        if self.model:
            argv += ["--model", self.model]
        started = time.time()
        try:
            res = self._run(argv, input=prompt, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=self.timeout_s)
        except FileNotFoundError:
            raise LLMError("not_configured", f"Claude Code not found ({self.executable})") from None
        except subprocess.TimeoutExpired:
            raise LLMError("timeout", f"claude -p timed out after {self.timeout_s}s",
                           retryable=True) from None
        try:
            body = json.loads((res.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise LLMError("provider_error", f"claude -p rc={res.returncode}: "
                           f"{(res.stderr or res.stdout or '')[-300:]}", retryable=True) from None
        if body.get("is_error") or res.returncode != 0:
            msg = str(body.get("result") or res.stderr or "")[-300:]
            kind = "auth" if re.search(r"log ?in|auth|credential", msg, re.I) else "provider_error"
            raise LLMError(kind, f"claude -p: {msg}", retryable=kind != "auth")
        usage = body.get("usage") or {}
        used = list((body.get("modelUsage") or {}).keys())
        return Completion(text=str(body.get("result") or ""),
                          model=(used[0] if used else self.model or "claude-code-default"),
                          provider=self.name,
                          input_tokens=usage.get("input_tokens"),
                          output_tokens=usage.get("output_tokens"),
                          cost_usd=body.get("total_cost_usd"),
                          latency_s=time.time() - started)


def decode_kiro_stream(raw: str) -> str:
    """Undo kiro-cli's non-TTY rendering.

    Without a terminal, kiro-cli writes each streamed token on its own line and
    renders a token's leading spaces as newlines, so a run of k newlines stands
    for k-1 spaces. JSON strings escape real newlines, so for the structured
    output this module consumes the decoding is lossless. Status lines
    (``WARNING:``, the ``▸ Credits`` trailer) are dropped first.
    """
    lines = [ln for ln in raw.split("\n")
             if not ln.startswith("WARNING:") and not ln.lstrip().startswith("▸")]
    body = "\n".join(lines).strip("\n")
    if body.count("\n") < 3 * max(1, len(body.split())) // 4:
        return re.sub(r"^\s*>\s?", "", body, flags=re.M)   # ordinary text output
    return re.sub(r"\n+", lambda m: " " * (len(m.group(0)) - 1), body)


# --------------------------------------------------------------- the client --
class LLMClient:
    """What the rest of SysPerf uses: budget, local rate limit, JSON validation."""

    def __init__(self, provider: Provider, meter: UsageMeter | None = None,
                 min_interval_s: float = 2.0, on_call: Callable | None = None,
                 clock=time.monotonic, sleep=time.sleep):
        self.provider = provider
        self.meter = meter or UsageMeter()
        self.min_interval_s = min_interval_s
        self.on_call = on_call
        self._last = 0.0
        self._lock = threading.Lock()
        self._clock, self._sleep = clock, sleep
        self.last_error: dict | None = None

    @property
    def available(self) -> bool:
        # A selected provider without credentials is not usable: reporting it as
        # available made every cycle fail with no_credentials.
        return (type(self.provider) is not Provider
                and getattr(self.provider, "configured", True))

    def complete(self, system: str, prompt: str, max_tokens: int = 4000) -> Completion:
        over = self.meter.check()
        if over:
            raise LLMError("budget", over)
        with self._lock:
            gap = self.min_interval_s - (self._clock() - self._last)
            if gap > 0:
                self._sleep(gap)
            self._last = self._clock()
        try:
            c = self.provider.complete(system, prompt, max_tokens)
        except LLMError as exc:
            self.last_error = {"kind": exc.kind, "message": str(exc), "ts": time.time()}
            self._record(None, exc)
            raise
        self.meter.add(c)
        self.last_error = None
        self._record(c, None)
        return c

    def complete_json(self, system: str, prompt: str, schema: dict,
                      max_tokens: int = 4000) -> tuple:
        """(validated object, Completion). Raises LLMError('invalid_output')."""
        c = self.complete(system, prompt, max_tokens)
        obj = extract_json(c.text)
        if obj is None:
            raise LLMError("invalid_output", "model output contained no JSON object")
        problems = validate(obj, schema)
        if problems:
            raise LLMError("invalid_output", "model output failed validation: "
                           + "; ".join(problems[:5]))
        return obj, c

    def _record(self, c: Completion | None, err: LLMError | None) -> None:
        if self.on_call:
            try:
                self.on_call(c, err, self.provider)
            except Exception:        # pragma: no cover - telemetry must not break calls
                log.exception("llm on_call hook failed")


def extract_json(text: str) -> dict | None:
    """First balanced top-level JSON object in ``text`` (fences tolerated)."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    depth, start, in_str, esc = 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
                in_str = False
            elif ch != "\\":
                esc = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start:i + 1])
                start = None
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def validate(obj: Any, schema: dict, path: str = "$") -> list:
    """Minimal JSON-schema subset: type, required, properties, items, enum,
    maxLength, maxItems, additionalProperties=False. Returns problems."""
    probs = []
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "number": (int, float), "boolean": bool, "null": type(None)}
    if t:
        allowed = t if isinstance(t, list) else [t]
        if not any(isinstance(obj, types[x]) and not (x in ("integer", "number")
                   and isinstance(obj, bool)) for x in allowed):
            return [f"{path}: expected {t}"]
    if "enum" in schema and obj not in schema["enum"]:
        probs.append(f"{path}: {obj!r} not in {schema['enum']}")
    if isinstance(obj, str) and "maxLength" in schema and len(obj) > schema["maxLength"]:
        probs.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(obj, list):
        if "maxItems" in schema and len(obj) > schema["maxItems"]:
            probs.append(f"{path}: more than {schema['maxItems']} items")
        if "minItems" in schema and len(obj) < schema["minItems"]:
            probs.append(f"{path}: fewer than {schema['minItems']} items")
        for i, item in enumerate(obj):
            probs += validate(item, schema.get("items", {}), f"{path}[{i}]")
    if isinstance(obj, dict):
        for req in schema.get("required", []):
            if req not in obj:
                probs.append(f"{path}: missing '{req}'")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(obj) - set(props)
            if extra:
                probs.append(f"{path}: unexpected keys {sorted(extra)}")
        for k, sub in props.items():
            if k in obj:
                probs += validate(obj[k], sub, f"{path}.{k}")
    return probs


def build_provider(settings: dict, key_file: Path | None) -> Provider:
    """Provider from the ``llm`` settings block. Unknown/empty -> offline."""
    s = settings or {}
    kind = (s.get("provider") or "none").lower()
    if kind == "anthropic":
        key, _src = load_api_key(key_file)
        return AnthropicProvider(key, s.get("model") or "auto",
                                 base_url=s.get("base_url") or DEFAULT_BASE_URL,
                                 timeout_s=float(s.get("timeout_s", 60)),
                                 max_retries=int(s.get("max_retries", 4)),
                                 prices=s.get("prices") or {})
    if kind == "claude_code":
        return ClaudeCodeProvider(model=s.get("model") or "auto",
                                  timeout_s=float(s.get("timeout_s", 600)),
                                  via=str(s.get("claude_via") or "auto"))
    if kind == "kiro_cli":
        return KiroCliProvider(model=s.get("model") or None,
                               timeout_s=float(s.get("timeout_s", 300)))
    return Provider()
