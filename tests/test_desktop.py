"""Full-coverage tests for desktop.py (the Microsoft Store app entry point)."""
import io
import json
import socket
import types

import sysperf.desktop as desktop


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _opener(body):
    def op(url, timeout):
        assert url.endswith("/api/health")
        if isinstance(body, Exception):
            raise body
        return _Resp(body.encode())
    return op


def test_is_sysperf_checks_the_response_shape():
    ok = json.dumps({"version": "4.1.0", "data_dir": "x", "ok": True})
    assert desktop.is_sysperf(5056, opener=_opener(ok))
    assert not desktop.is_sysperf(5056, opener=_opener(json.dumps({"status": "up"})))
    assert not desktop.is_sysperf(5056, opener=_opener("[1, 2]"))
    assert not desktop.is_sysperf(5056, opener=_opener("<html>"))
    assert not desktop.is_sysperf(5056, opener=_opener(OSError("refused")))


def test_port_free_and_free_port_use_real_sockets():
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    busy = held.getsockname()[1]
    try:
        assert desktop.port_free(busy) is False
        p = desktop.free_port()
        assert p > 0 and p != busy and desktop.port_free(p)
    finally:
        held.close()


def test_configured_port():
    assert desktop.configured_port({}) == 5056
    assert desktop.configured_port({"PERFDASH_PORT": "5100"}) == 5100
    assert desktop.configured_port({"PERFDASH_PORT": "nope"}) == 5056


def test_choose_attaches_serves_or_moves_aside():
    assert desktop.choose({}, probe=lambda p: True) == (5056, True)
    assert desktop.choose({}, probe=lambda p: False, free=lambda p: True) == (5056, False)
    assert desktop.choose({}, probe=lambda p: False, free=lambda p: False,
                          pick=lambda: 61234) == (61234, False)


def test_start_server_runs_cli_on_a_daemon_thread_without_a_browser():
    seen = {}

    class T:
        def __init__(self, target, args, name, daemon):
            seen.update(target=target, args=args, daemon=daemon)

        def start(self):
            seen["started"] = True

    env = {}
    desktop.start_server(5100, env, cli_main="CLI", thread_cls=T)
    assert seen == {"target": "CLI", "args": (["--port", "5100"],), "daemon": True,
                    "started": True}
    assert env["SYSPERF_NO_BROWSER"] == "1"


def test_wait_ready():
    t = [0.0]
    sleeps = []
    calls = iter([False, False, True])
    assert desktop.wait_ready(1, probe=lambda p: next(calls), clock=lambda: t[0],
                              sleep=sleeps.append)
    assert sleeps == [0.5, 0.5]

    def clock():
        t[0] += 30
        return t[0]
    assert not desktop.wait_ready(1, probe=lambda p: False, clock=clock, sleep=lambda s: None)


def test_profile_dir():
    assert desktop.profile_dir({"SYSPERF_DATA_DIR": "/d"}).replace("\\", "/") == "/d/webview"
    assert desktop.profile_dir({"LOCALAPPDATA": "/l"}).replace("\\", "/") == "/l/SysPerf/webview"
    assert desktop.profile_dir({}).endswith("webview")


def test_error_page_names_the_port():
    assert "127.0.0.1:5100" in desktop.error_page(5100)


class _WebView:
    def __init__(self):
        self.windows, self.started = [], None

    def create_window(self, title, url=None, html=None, **kw):
        self.windows.append({"title": title, "url": url, "html": html, **kw})
        self.destroyed = []
        return types.SimpleNamespace(destroy=lambda: self.destroyed.append(title))

    def start(self, **kw):
        self.started = kw


def test_main_attaches_to_a_running_dashboard(tmp_path):
    wv, served = _WebView(), []
    env = {"SYSPERF_DATA_DIR": str(tmp_path)}
    assert desktop.main(env=env, webview=wv, probe=lambda p: True,
                        server=lambda p, e: served.append(p)) == 0
    assert served == [] and wv.windows[0]["url"] == "http://127.0.0.1:5056/#home"
    assert wv.started["storage_path"] == str(tmp_path / "webview")
    assert (tmp_path / "webview").is_dir()


def test_main_starts_its_own_dashboard(tmp_path):
    wv, served = _WebView(), []
    desktop.main(env={"SYSPERF_DATA_DIR": str(tmp_path)}, webview=wv,
                 probe=lambda p: False, free=lambda p: False, pick=lambda: 61000,
                 server=lambda p, e: served.append(p), ready=lambda p, probe: True)
    assert served == [61000] and wv.windows[0]["url"] == "http://127.0.0.1:61000/#home"
    assert wv.windows[0]["min_size"] == (960, 640)


def test_main_shows_an_error_when_the_dashboard_never_answers(tmp_path):
    wv = _WebView()
    desktop.main(env={"SYSPERF_DATA_DIR": str(tmp_path)}, webview=wv,
                 probe=lambda p: False, free=lambda p: True,
                 server=lambda p, e: None, ready=lambda p, probe: False)
    assert wv.windows[0]["url"] is None and "could not start" in wv.windows[0]["html"]


def test_wait_released():
    t = [0.0]
    sleeps = []
    frees = iter([False, True])
    assert desktop.wait_released(1, free=lambda p: next(frees), clock=lambda: t[0],
                                 sleep=sleeps.append)
    assert sleeps == [0.25]

    def clock():
        t[0] += 30
        return t[0]
    assert not desktop.wait_released(1, free=lambda p: False, clock=clock, sleep=lambda s: None)


def test_relaunch_waits_for_the_old_instance_before_choosing(tmp_path):
    order = []
    wv = _WebView()
    desktop.main(["--relaunch"], env={"SYSPERF_DATA_DIR": str(tmp_path)}, webview=wv,
                 probe=lambda p: order.append("probe") or False, free=lambda p: True,
                 server=lambda p, e: None, ready=lambda p, probe: True,
                 released=lambda p, free: order.append(("released", p)))
    assert order[0] == ("released", 5056) and "probe" in order


def test_main_closes_the_window_when_windows_stages_an_update(tmp_path):
    # 5.4.2 live: every Store update failed once with 0x80073D02 while the window
    # was open; the window now hands its close to the update watcher
    wv, watched = _WebView(), []
    desktop.main(env={"SYSPERF_DATA_DIR": str(tmp_path)}, webview=wv, probe=lambda p: True,
                 watch_update=watched.append)
    watched[0]()
    assert wv.destroyed == ["SysPerf"]
