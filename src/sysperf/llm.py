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

import json
import logging
import os
import random
import re
import stat
import subprocess
from .background_process import run_hidden
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("sysperf.llm")

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
def load_api_key(key_file: Optional[Path], env=None) -> tuple:
    """(key or None, source). Refuses a key file other users can read."""
    env = os.environ if env is None else env
    for var in ("SYSPERF_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"):
        if env.get(var):
            return env[var].strip(), f"env:{var}"
    if key_file and Path(key_file).is_file():
        p = Path(key_file)
        if os.name != "nt" and (p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
            log.warning("ignoring %s: readable by other users (chmod 600 it)", p.name)
            return None, "file:insecure-permissions"
        key = p.read_text(encoding="utf-8").strip()
        return (key or None), "file"
    return None, "none"


def store_api_key(key_file: Path, key: str) -> None:
    """Write the key with owner-only permissions (atomic, never logged)."""
    key = (key or "").strip()
    if not key or len(key) > 512 or any(c.isspace() for c in key):
        raise ValueError("API key must be a single non-empty token")
    key_file = Path(key_file)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(key_file.parent, 0o700)
    except OSError:                  # pragma: no cover - e.g. Windows
        pass
    tmp = key_file.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key)
    os.replace(tmp, key_file)


# ------------------------------------------------------------------- results --
@dataclass
class Completion:
    text: str
    model: str
    provider: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
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

    def check(self) -> Optional[str]:
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


def estimate_cost(prices: dict, model: str, tin: Optional[int],
                  tout: Optional[int]) -> Optional[float]:
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


class AnthropicProvider(Provider):
    """Anthropic Messages API with timeout, retries, backoff and 429 handling."""
    name = "anthropic"

    def __init__(self, api_key: Optional[str], model: Optional[str],
                 base_url: str = DEFAULT_BASE_URL, timeout_s: float = 60.0,
                 max_retries: int = 4, backoff_base_s: float = 1.0,
                 prices: Optional[dict] = None, opener=None, sleep=time.sleep):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.prices = prices or {}
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> tuple:
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
                    raise LLMError("auth", f"credentials rejected (HTTP {code})")
                if code == 404:
                    raise LLMError("not_found", f"HTTP 404 — unknown model or endpoint: {detail}")
                if code in (400, 413, 422):
                    raise LLMError("bad_request", f"HTTP {code}: {detail}")
                retryable = code == 429 or code >= 500
                kind = "rate_limited" if code == 429 else "server_error"
                if not retryable or attempt > self.max_retries:
                    raise LLMError(kind, f"HTTP {code} after {attempt} attempt(s): {detail}",
                                   retryable=retryable)
                wait = self._retry_after(exc) or self._backoff(attempt)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt > self.max_retries:
                    raise LLMError("network", f"unreachable after {attempt} attempt(s): "
                                   f"{getattr(exc, 'reason', exc)}", retryable=True)
                wait = self._backoff(attempt)
            except ValueError as exc:
                raise LLMError("bad_response", f"non-JSON response: {exc}")
            log.info("anthropic: retrying in %.1fs (attempt %d)", wait, attempt)
            self._sleep(wait)

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped at 30 s."""
        return min(30.0, self.backoff_base_s * (2 ** (attempt - 1))) * (0.5 + random.random() / 2)

    @staticmethod
    def _retry_after(exc) -> Optional[float]:
        try:
            v = exc.headers.get("retry-after")
            return min(60.0, float(v)) if v else None
        except (TypeError, ValueError, AttributeError):
            return None

    def complete(self, system: str, prompt: str, max_tokens: int) -> Completion:
        if not self.model:
            raise LLMError("not_configured", "no model configured (choose one from the "
                           "provider's model list)")
        started = time.time()
        body, attempts = self._request("POST", "/v1/messages", {
            "model": self.model, "max_tokens": int(max_tokens), "system": system,
            "messages": [{"role": "user", "content": prompt}]})
        try:
            text = "".join(b.get("text", "") for b in body.get("content", [])
                           if b.get("type") == "text")
            usage = body.get("usage") or {}
            tin, tout = usage.get("input_tokens"), usage.get("output_tokens")
        except (AttributeError, TypeError) as exc:
            raise LLMError("bad_response", f"unexpected response shape: {exc}")
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

    def __init__(self, model: Optional[str] = None, executable: str = "kiro-cli",
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
            raise LLMError("not_configured", f"{self.executable} not found")
        except subprocess.TimeoutExpired:
            raise LLMError("timeout", f"kiro-cli timed out after {self.timeout_s}s",
                           retryable=True)
        out = _ANSI.sub("", res.stdout or "")
        if res.returncode != 0 or not out.strip():
            raise LLMError("provider_error", f"kiro-cli rc={res.returncode}: "
                           f"{_ANSI.sub('', res.stderr or '')[-300:]}", retryable=True)
        out = decode_kiro_stream(out)
        return Completion(text=out, model=self.model or "kiro-default",
                          provider=self.name, latency_s=time.time() - started)


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

    def __init__(self, provider: Provider, meter: Optional[UsageMeter] = None,
                 min_interval_s: float = 2.0, on_call: Optional[Callable] = None,
                 clock=time.monotonic, sleep=time.sleep):
        self.provider = provider
        self.meter = meter or UsageMeter()
        self.min_interval_s = min_interval_s
        self.on_call = on_call
        self._last = 0.0
        self._lock = threading.Lock()
        self._clock, self._sleep = clock, sleep
        self.last_error: Optional[dict] = None

    @property
    def available(self) -> bool:
        return type(self.provider) is not Provider

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

    def _record(self, c: Optional[Completion], err: Optional[LLMError]) -> None:
        if self.on_call:
            try:
                self.on_call(c, err, self.provider)
            except Exception:        # pragma: no cover - telemetry must not break calls
                log.exception("llm on_call hook failed")


def extract_json(text: str) -> Optional[dict]:
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


def build_provider(settings: dict, key_file: Optional[Path]) -> Provider:
    """Provider from the ``llm`` settings block. Unknown/empty -> offline."""
    s = settings or {}
    kind = (s.get("provider") or "none").lower()
    if kind == "anthropic":
        key, _src = load_api_key(key_file)
        return AnthropicProvider(key, s.get("model") or None,
                                 base_url=s.get("base_url") or DEFAULT_BASE_URL,
                                 timeout_s=float(s.get("timeout_s", 60)),
                                 max_retries=int(s.get("max_retries", 4)),
                                 prices=s.get("prices") or {})
    if kind == "kiro_cli":
        return KiroCliProvider(model=s.get("model") or None,
                               timeout_s=float(s.get("timeout_s", 300)))
    return Provider()
