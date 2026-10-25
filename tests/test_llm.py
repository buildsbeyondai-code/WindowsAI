"""4.0 runtime LLM layer: client resilience, credentials, validation, budget."""
import base64
import io
import json
import logging
import os
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import sysperf.llm as llm


# ---------------------------------------------------------------- fake API --
class _Handler(BaseHTTPRequestHandler):
    script = []          # list of (status, headers, body) consumed per request
    seen = []

    def _serve(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        _Handler.seen.append({"path": self.path, "key": self.headers.get("x-api-key"),
                              "version": self.headers.get("anthropic-version"),
                              "body": json.loads(body) if body else None})
        status, headers, payload = _Handler.script.pop(0) if _Handler.script else (500, {}, {})
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    do_GET = do_POST = _serve

    def log_message(self, *a):
        pass


@pytest.fixture
def api():
    _Handler.script, _Handler.seen = [], []
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _ok(text="hi", model="m-test"):
    return (200, {}, {"model": model, "content": [{"type": "text", "text": text}],
                      "usage": {"input_tokens": 10, "output_tokens": 5}})


def _prov(api, **kw):
    return llm.AnthropicProvider("sk-ant-test-0123456789", kw.pop("model", "m-test"),
                                 base_url=api, sleep=lambda s: None, **kw)


def test_completion_sends_headers_and_counts_tokens(api):
    _Handler.script = [_ok()]
    c = _prov(api, prices={"m-test": [3.0, 15.0]}).complete("sys", "hello", 100)
    assert c.text == "hi" and c.input_tokens == 10 and c.output_tokens == 5
    assert c.cost_usd == pytest.approx((10 * 3 + 5 * 15) / 1e6)
    req = _Handler.seen[0]
    assert req["path"] == "/v1/messages" and req["version"] == llm.ANTHROPIC_VERSION
    assert req["body"]["model"] == "m-test" and req["body"]["system"] == "sys"


def test_rate_limit_honours_retry_after_then_succeeds(api):
    waits = []
    _Handler.script = [(429, {"retry-after": "2"}, {}), (529, {}, {}), _ok()]
    p = llm.AnthropicProvider("sk-ant-test-0123456789", "m", base_url=api,
                              sleep=waits.append)
    c = p.complete("s", "p", 10)
    assert c.attempts == 3 and waits[0] == 2.0 and 0 < waits[1] <= 30


def test_retries_are_bounded(api):
    _Handler.script = [(500, {}, {})] * 10
    with pytest.raises(llm.LLMError) as e:
        _prov(api, max_retries=2).complete("s", "p", 10)
    assert e.value.kind == "server_error" and len(_Handler.seen) == 3


def test_auth_failure_is_not_retried_and_hides_key(api):
    _Handler.script = [(401, {}, {"error": "sk-ant-test-0123456789 invalid"})]
    with pytest.raises(llm.LLMError) as e:
        _prov(api).complete("s", "p", 10)
    assert e.value.kind == "auth" and len(_Handler.seen) == 1
    assert "0123456789" not in str(e.value)


def test_network_down_is_an_error_not_a_crash():
    p = llm.AnthropicProvider("sk-ant-x-12345678", "m", base_url="http://127.0.0.1:9",
                              max_retries=1, timeout_s=1, sleep=lambda s: None)
    with pytest.raises(llm.LLMError) as e:
        p.complete("s", "p", 10)
    assert e.value.kind == "network" and e.value.retryable


def test_timeout_is_retried():
    calls = []

    def opener(req, timeout):
        calls.append(timeout)
        raise TimeoutError("slow")
    p = llm.AnthropicProvider("sk-ant-x-12345678", "m", opener=opener, max_retries=2,
                              timeout_s=7, sleep=lambda s: None)
    with pytest.raises(llm.LLMError):
        p.complete("s", "p", 10)
    assert calls == [7, 7, 7]


def test_models_are_listed_not_hard_coded(api):
    _Handler.script = [(200, {}, {"data": [{"id": "model-a"}, {"id": "model-b"}]})]
    assert _prov(api).list_models() == ["model-a", "model-b"]
    assert _Handler.seen[0]["path"].startswith("/v1/models")


def test_no_model_and_no_key_are_explicit():
    with pytest.raises(llm.LLMError) as e:
        llm.AnthropicProvider("k" * 20, None).complete("s", "p", 1)
    assert e.value.kind == "not_configured"
    with pytest.raises(llm.LLMError) as e:
        llm.AnthropicProvider(None, "m").complete("s", "p", 1)
    assert e.value.kind == "no_credentials"


def test_offline_provider_is_unavailable():
    c = llm.LLMClient(llm.build_provider({}, None))
    assert not c.available
    with pytest.raises(llm.LLMError):
        c.complete("s", "p")


# ------------------------------------------------------------- credentials --
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission-bit enforcement; Windows uses ACLs")
def test_key_file_is_private_and_insecure_file_refused(tmp_path):
    f = tmp_path / "s" / "anthropic.key"
    llm.store_api_key(f, "sk-ant-abc-0123456789")
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert llm.load_api_key(f, env={}) == ("sk-ant-abc-0123456789", "file")
    os.chmod(f, 0o644)
    assert llm.load_api_key(f, env={})[0] is None
    assert llm.load_api_key(f, env={"ANTHROPIC_API_KEY": "sk-ant-env-1"})[1].startswith("env")
    with pytest.raises(ValueError):
        llm.store_api_key(f, "two words")


class _FakeDpapi:
    """Reversible stand-in for DPAPI: the key never appears in the file."""

    def protect(self, data):
        return bytes(b ^ 0x5A for b in data)

    def unprotect(self, blob):
        if blob.startswith(b"bad"):
            raise OSError(13, "not this user")
        return bytes(b ^ 0x5A for b in blob)


def test_key_file_is_dpapi_encrypted_when_available(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_dpapi", _FakeDpapi)
    f = tmp_path / "s" / "anthropic.key"
    llm.store_api_key(f, "sk-ant-abc-0123456789")
    text = f.read_text(encoding="utf-8")
    assert text.startswith(llm.DPAPI_PREFIX) and "sk-ant" not in text
    assert llm.load_api_key(f, env={}) == ("sk-ant-abc-0123456789", "file:dpapi")


def test_plaintext_key_file_is_encrypted_in_place_on_first_read(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "OS_NAME", "nt")          # skip the POSIX mode check
    monkeypatch.setattr(llm, "_dpapi", _FakeDpapi)
    f = tmp_path / "anthropic.key"
    f.write_text("sk-ant-legacy-0123456789\n", encoding="utf-8")
    assert llm.load_api_key(f, env={}) == ("sk-ant-legacy-0123456789", "file:dpapi")
    assert f.read_text(encoding="utf-8").startswith(llm.DPAPI_PREFIX)
    assert llm.load_api_key(f, env={})[0] == "sk-ant-legacy-0123456789"


def test_plaintext_key_still_loads_when_encrypting_fails(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(llm, "OS_NAME", "nt")
    monkeypatch.setattr(llm, "_dpapi", _FakeDpapi)

    def fail(path, key):
        raise OSError(5, "read-only")
    monkeypatch.setattr(llm, "store_api_key", fail)
    f = tmp_path / "anthropic.key"
    f.write_text("sk-ant-legacy-0123456789", encoding="utf-8")
    assert llm.load_api_key(f, env={}) == ("sk-ant-legacy-0123456789", "file")
    assert "could not encrypt" in caplog.text


@pytest.mark.parametrize("blob, dpapi, why", [
    ("bad-blob", _FakeDpapi, "cannot decrypt"),         # another user's blob
    ("!!not base64!!", _FakeDpapi, "cannot decrypt"),
    ("AAAA", lambda: None, "not Windows"),               # copied to another OS
])
def test_unreadable_dpapi_key_file_is_ignored(tmp_path, monkeypatch, caplog, blob, dpapi, why):
    monkeypatch.setattr(llm, "OS_NAME", "nt")
    monkeypatch.setattr(llm, "_dpapi", dpapi)
    f = tmp_path / "anthropic.key"
    raw = base64.b64encode(b"bad").decode() if blob == "bad-blob" else blob
    f.write_text(llm.DPAPI_PREFIX + raw, encoding="utf-8")
    assert llm.load_api_key(f, env={}) == (None, "file:unreadable")
    assert why in caplog.text


def test_dpapi_is_only_used_on_windows(monkeypatch):
    monkeypatch.setattr(llm, "OS_NAME", "posix")
    assert llm._dpapi() is None


def test_logs_never_carry_a_key():
    stream = io.StringIO()
    h = logging.StreamHandler(stream)
    h.addFilter(llm.RedactingFilter())
    lg = logging.getLogger("redaction-test")
    lg.addHandler(h)
    lg.warning("calling with %s", "sk-ant-api03-SECRETSECRETSECRET")
    assert "SECRET" not in stream.getvalue() and "sk-ant-***" in stream.getvalue()


# -------------------------------------------------- output validation, budget --
SCHEMA = {"type": "object", "required": ["a"], "additionalProperties": False,
          "properties": {"a": {"type": "integer"}, "b": {"type": "array", "maxItems": 1}}}


@pytest.mark.parametrize("text,ok", [
    ('{"a": 1}', True), ('noise ```json\n{"a": 2}\n``` tail', True),
    ('before {"a": 3, "b": [1]} after', True), ('{"a": "x"}', False),
    ('{"a": 1, "evil": 1}', False), ('{"b": [1, 2]}', False), ("no json", False),
    ('{"a": true}', False)])
def test_structured_output_validation(text, ok):
    class P(llm.Provider):
        name, model = "p", "m"

        def complete(self, s, p, m):
            return llm.Completion(text=text, model="m", provider="p")
    c = llm.LLMClient(P(), min_interval_s=0)
    if ok:
        assert c.complete_json("s", "p", SCHEMA)[0]["a"] in (1, 2, 3)
    else:
        with pytest.raises(llm.LLMError) as e:
            c.complete_json("s", "p", SCHEMA)
        assert e.value.kind == "invalid_output"


def test_budget_stops_calls():
    class P(llm.Provider):
        name, model = "p", "m"

        def complete(self, s, p, m):
            return llm.Completion(text="x", model="m", provider="p", input_tokens=60,
                                  output_tokens=50)
    c = llm.LLMClient(P(), llm.UsageMeter(daily_token_budget=100), min_interval_s=0)
    c.complete("s", "p")
    with pytest.raises(llm.LLMError) as e:
        c.complete("s", "p")
    assert e.value.kind == "budget"


def test_kiro_stream_decoding():
    raw = ('WARNING: --trust-tools arg\n{\n"title"\n:\n\n"Use\n\na\n\nset",\n\n"find"\n:\n\n'
           '"\n\n\n\n\nseen\n\n=\n\n[\n]"\n}\n\n ▸ Credits: 0.3\n')
    obj = llm.extract_json(llm.decode_kiro_stream(raw))
    assert obj == {"title": "Use a set", "find": "    seen = []"}
    assert llm.decode_kiro_stream("> plain answer\n") == "plain answer"


def test_kiro_provider_trusts_no_tools():
    seen = {}

    def run(argv, **kw):
        seen["argv"] = argv

        class R:
            returncode, stdout, stderr = 0, '{"ok": true}', ""
        return R()
    llm.KiroCliProvider(run=run).complete("s", "p", 10)
    assert "--trust-tools=" in seen["argv"] and "--trust-all-tools" not in seen["argv"]
    assert "-a" not in seen["argv"]


def test_http_error_body_read_failure_is_tolerated():
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b"oops"))
    with pytest.raises(llm.LLMError) as e:
        llm.AnthropicProvider("sk-ant-x-12345678", "m", opener=opener).complete("s", "p", 1)
    assert e.value.kind == "bad_request"
