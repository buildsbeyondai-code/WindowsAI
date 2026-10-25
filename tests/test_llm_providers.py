"""The remaining LLM paths: the Claude Code provider (the one this installation
uses), auto model selection and retirement, kiro-cli failures, the meter's
snapshot and cost budget, redaction of extra secrets and provider building."""
import json
import subprocess
from types import SimpleNamespace

import pytest
from tests import test_llm as base

import sysperf.llm as llm

_ok, _prov, _Handler = base._ok, base._prov, base._Handler
api = base.api                                # the fake-API fixture


def _res(stdout="", rc=0, stderr=""):
    return SimpleNamespace(stdout=stdout, returncode=rc, stderr=stderr)


# ------------------------------------------------------------- claude code --
def _cc(run, **kw):
    kw.setdefault("via", "direct")
    kw.setdefault("executable", "claude")
    return llm.ClaudeCodeProvider(run=run, **kw)


def test_claude_code_runs_headless_with_no_tools_and_parses_usage():
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, kw=kw)
        return _res(json.dumps({"result": '{"ok": true}', "usage": {
            "input_tokens": 12, "output_tokens": 3}, "total_cost_usd": 0.01,
            "modelUsage": {"claude-opus-5-5": {}}}))
    c = _cc(run, model="opus").complete("SYS", "PROMPT", 100)
    assert seen["argv"][:4] == ["claude", "-p", "--tools", ""]
    assert "--no-session-persistence" in seen["argv"] and seen["argv"][-2:] == ["--model",
                                                                                 "opus"]
    assert seen["kw"]["input"] == "PROMPT"
    assert c.text == '{"ok": true}' and c.model == "claude-opus-5-5"
    assert (c.input_tokens, c.output_tokens, c.cost_usd) == (12, 3, 0.01)


def test_claude_code_auto_model_and_default_name():
    p = _cc(lambda argv, **kw: _res(json.dumps({"result": "x"})), model="auto")
    c = p.complete("s", "p", 10)
    assert p.model is None and c.model == "claude-code-default"
    assert p.list_models()[0] == "auto"


def test_claude_code_over_wsl_puts_tools_flag_inside_the_script():
    seen = {}

    def run(argv, **kw):
        seen["argv"] = argv
        return _res(json.dumps({"result": "x"}))
    _cc(run, via="wsl", executable=None).complete("s", "p", 10)
    assert seen["argv"][:3] == ["wsl.exe", "-e", "bash"] and '--tools ""' in seen["argv"][4]


@pytest.mark.parametrize("exc,kind", [(FileNotFoundError(), "not_configured"),
                                      (subprocess.TimeoutExpired("c", 1), "timeout")])
def test_claude_code_launch_failures(exc, kind):
    def run(argv, **kw):
        raise exc
    with pytest.raises(llm.LLMError) as e:
        _cc(run).complete("s", "p", 10)
    assert e.value.kind == kind


@pytest.mark.parametrize("res,kind,retry", [
    (_res("not json", rc=1, stderr="boom"), "provider_error", True),
    (_res("", rc=1), "provider_error", True),
    (_res(json.dumps({"is_error": True, "result": "Please run /login"})), "auth", False),
    (_res(json.dumps({"result": "overloaded"}), rc=1), "provider_error", True)])
def test_claude_code_error_classification(res, kind, retry):
    with pytest.raises(llm.LLMError) as e:
        _cc(lambda argv, **kw: res).complete("s", "p", 10)
    assert e.value.kind == kind and e.value.retryable is retry


def test_claude_code_discovery(monkeypatch, tmp_path):
    import shutil
    monkeypatch.setattr(llm, "OS_NAME", "nt")
    p = llm.ClaudeCodeProvider(run=lambda argv, **kw: _res(rc=0))
    assert p.via_wsl and p.executable == "wsl.exe"
    p = llm.ClaudeCodeProvider(run=lambda argv, **kw: _res(rc=1))
    assert not p.via_wsl

    def boom(argv, **kw):
        raise OSError("no wsl")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(llm.ClaudeCodeProvider, "WINGET_LINK", str(tmp_path / "missing.exe"))
    p = llm.ClaudeCodeProvider(run=boom)
    assert not p.via_wsl and p.executable == "claude" and not p.configured
    link = tmp_path / "claude.exe"
    link.write_text("")
    monkeypatch.setattr(llm.ClaudeCodeProvider, "WINGET_LINK", str(link))
    assert llm.ClaudeCodeProvider(run=boom).executable == str(link)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    p = llm.ClaudeCodeProvider(run=boom)
    assert p.executable == "/usr/bin/claude" and p.configured


# ----------------------------------------------------------- anthropic --
def test_auto_model_is_resolved_and_retired_models_are_replaced(api):
    _Handler.script = [(200, {}, {"data": [{"id": "claude-haiku-9"}, {"id": "claude-opus-9"}]}),
                       (404, {}, {"error": "model retired"}),
                       (200, {}, {"data": [{"id": "claude-opus-10"}]}),
                       _ok(model="claude-opus-10")]
    p = _prov(api, model="auto")
    c = p.complete("s", "p", 10)
    assert c.model == "claude-opus-10" and p.model == "claude-opus-10"
    assert _Handler.seen[-1]["body"]["model"] == "claude-opus-10"


def test_auto_selection_without_models_and_404_when_explicit(api):
    _Handler.script = [(200, {}, {"data": []})]
    with pytest.raises(llm.LLMError, match="listed no models"):
        _prov(api, model="auto").complete("s", "p", 10)
    _Handler.script = [(404, {}, {"error": "nope"})]
    with pytest.raises(llm.LLMError) as e:
        _prov(api).complete("s", "p", 10)
    assert e.value.kind == "not_found"


def test_pick_auto_model_preferences():
    assert llm.pick_auto_model(["a-haiku", "b-sonnet", "c-opus"]) == "c-opus"
    assert llm.pick_auto_model(["x-sonnet", "y-haiku"]) == "x-sonnet"
    assert llm.pick_auto_model(["only-one"]) == "only-one"
    assert llm.pick_auto_model([]) is None


def test_non_json_and_odd_shapes(monkeypatch):
    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body
    p = llm.AnthropicProvider("sk-ant-k-0123456789", "m", opener=lambda r, timeout: Resp(
        b"<html>"), sleep=lambda s: None)
    with pytest.raises(llm.LLMError) as e:
        p.complete("s", "p", 10)
    assert e.value.kind == "bad_response"
    p._open = lambda r, timeout: Resp(json.dumps({"content": "not-a-list"}).encode())
    with pytest.raises(llm.LLMError, match="unexpected response shape"):
        p.complete("s", "p", 10)
    assert p.configured and not llm.AnthropicProvider(None, "m").configured
    bad = SimpleNamespace(headers={"retry-after": "soon"})
    assert llm.AnthropicProvider._retry_after(bad) is None


# --------------------------------------------------------------- kiro --
@pytest.mark.parametrize("exc,kind", [(FileNotFoundError(), "not_configured"),
                                      (subprocess.TimeoutExpired("k", 1), "timeout")])
def test_kiro_launch_failures(exc, kind):
    def run(argv, **kw):
        raise exc
    with pytest.raises(llm.LLMError) as e:
        llm.KiroCliProvider(model="m", run=run).complete("s", "p", 10)
    assert e.value.kind == kind


def test_kiro_nonzero_exit_and_model_flag():
    seen = {}

    def run(argv, **kw):
        seen["argv"] = argv
        return _res("", rc=3, stderr="\x1b[31mfail\x1b[0m")
    with pytest.raises(llm.LLMError, match="rc=3: fail"):
        llm.KiroCliProvider(model="claude-x", run=run).complete("s", "p", 10)
    assert seen["argv"][4:6] == ["--model", "claude-x"]


# ------------------------------------------------- meter, redaction, client --
def test_meter_snapshot_and_cost_budget():
    m = llm.UsageMeter(daily_token_budget=0, daily_cost_budget_usd=0.05)
    m.add(llm.Completion(text="", model="m", provider="p", input_tokens=5, cost_usd=0.06))
    assert "daily cost budget exhausted" in m.check()
    snap = m.snapshot()
    assert snap["calls"] == 1 and snap["cost_usd"] == 0.06 and snap["tokens"] == 5


def test_redact_extra_secrets_and_build_provider_variants(tmp_path, monkeypatch):
    assert llm.redact("token=abcdefgh123 and x", ("abcdefgh123", "short")) == "token=*** and x"
    for var in ("SYSPERF_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert llm.load_api_key(tmp_path / "missing.key") == (None, "none")
    assert isinstance(llm.build_provider({"provider": "claude_code", "claude_via": "direct"},
                                         None), llm.ClaudeCodeProvider)
    assert isinstance(llm.build_provider({"provider": "kiro_cli"}, None), llm.KiroCliProvider)
    assert type(llm.build_provider({}, None)) is llm.Provider
    assert llm.Provider().list_models() == []


def test_client_sleeps_to_respect_the_local_rate_limit():
    slept = []
    clock = iter([10.0, 10.0, 10.5, 10.5])

    class P(llm.Provider):
        name = "p"

        def complete(self, system, prompt, max_tokens):
            return llm.Completion(text="{}", model="m", provider="p")
    c = llm.LLMClient(P(), min_interval_s=2.0, clock=lambda: next(clock), sleep=slept.append)
    c._last = 9.5
    c.complete("s", "p")
    assert slept and slept[0] == pytest.approx(1.5)


def test_client_availability_and_on_call_hook():
    calls = []

    class P(llm.Provider):
        name = "p"
        configured = True

        def complete(self, system, prompt, max_tokens):
            return llm.Completion(text="{}", model="m", provider="p")
    c = llm.LLMClient(P(), min_interval_s=0, on_call=lambda *a: calls.append(a))
    assert c.available
    c.complete("s", "p")
    assert calls and calls[0][1] is None
    assert not llm.LLMClient(llm.Provider()).available


def test_extract_json_and_validate_edges():
    assert llm.extract_json("") is None
    assert llm.extract_json("```json\n{bad}\n``` then {\"a\": 1}") == {"a": 1}
    schema = {"type": "object", "properties": {
        "k": {"type": "string", "enum": ["x"], "maxLength": 2},
        "l": {"type": "array", "minItems": 2}}}
    probs = llm.validate({"k": "zzz", "l": [1]}, schema)
    assert any("not in" in p for p in probs) and any("longer than" in p for p in probs)
    assert any("fewer than 2" in p for p in probs)


def test_build_anthropic_provider_reads_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSPERF_ANTHROPIC_API_KEY", "sk-ant-env-0123456789")
    p = llm.build_provider({"provider": "anthropic", "model": "m1", "prices": {"m1": [1, 2]}},
                           tmp_path / "k")
    assert isinstance(p, llm.AnthropicProvider) and p.api_key.startswith("sk-ant-env")
    assert p.model == "m1" and p.prices == {"m1": [1, 2]}
