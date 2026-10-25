"""The real DPAPI key-file encryption, on Windows only."""
import os

import pytest

from sysperf import llm

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")


def test_real_dpapi_round_trip_and_file_holds_no_key(tmp_path):
    dp = llm._WinDpapi()
    assert dp.unprotect(dp.protect(b"secret")) == b"secret"
    f = tmp_path / "anthropic.key"
    llm.store_api_key(f, "sk-ant-win-0123456789")
    assert "sk-ant" not in f.read_text(encoding="utf-8")
    assert llm.load_api_key(f, env={}) == ("sk-ant-win-0123456789", "file:dpapi")


def test_tampered_blob_is_refused(tmp_path):
    f = tmp_path / "anthropic.key"
    f.write_text(llm.DPAPI_PREFIX + "AAAAAAAA", encoding="utf-8")
    assert llm.load_api_key(f, env={}) == (None, "file:unreadable")
