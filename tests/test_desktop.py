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
                          pick=lambda: 61234, settle=lambda p: False) == (61234, False)


def test_choose_waits_for_a_sysperf_still_starting_on_a_busy_port():
    # 5.5.3: Store app and scripted install race at sign-in; the second must
    # attach to the first instead of running another collector elsewhere.
    assert desktop.choose({}, probe=lambda p: False, free=lambda p: False,
                          pick=lambda: 61234, settle=lambda p: True) == (5056, True)


def test_choose_default_settle_polls_the_probe(monkeypatch):
    seen = []
    monkeypatch.setattr(desktop, "wait_ready",
                        lambda p, probe, timeout: seen.append((p, timeout)) or True)
    assert desktop.choose({}, probe=lambda p: False, free=lambda p: False) == (5056, True)
    assert seen == [(5056, desktop.PORT_SETTLE_S)]


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
                 server=lambda p, e: served.append(p), ready=lambda p, probe: True,
                 settle=lambda p: False)
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


# ------------------------------------------ 5.5.3: title bar follows the theme --
def test_colorref_is_bgr():
    assert desktop.colorref("#0b0f17") == 0x170F0B
    assert desktop.colorref("ffffff") == 0xFFFFFF


def test_paint_titlebar_sets_dark_mode_caption_text_and_border():
    calls = []
    ok = desktop.paint_titlebar(42, "dark", set_attr=lambda h, a, v: calls.append((h, a, v)) or True)
    c = desktop.TITLEBAR["dark"]
    assert ok and calls == [
        (42, desktop.DWMWA_USE_IMMERSIVE_DARK_MODE, 1),
        (42, desktop.DWMWA_CAPTION_COLOR, desktop.colorref(c["caption"])),
        (42, desktop.DWMWA_TEXT_COLOR, desktop.colorref(c["text"])),
        (42, desktop.DWMWA_BORDER_COLOR, desktop.colorref(c["border"]))]
    assert not desktop.paint_titlebar(42, "light", set_attr=lambda *a: False)
    assert not desktop.paint_titlebar(None, "dark", set_attr=lambda *a: True)
    assert not desktop.paint_titlebar(42, "sepia", set_attr=lambda *a: True)


def test_window_hwnd_reads_the_winforms_handle():
    handle = types.SimpleNamespace(ToInt64=lambda: 1234)
    w = types.SimpleNamespace(native=types.SimpleNamespace(Handle=handle))
    assert desktop.window_hwnd(w) == 1234
    assert desktop.window_hwnd(types.SimpleNamespace(native=None)) is None


def test_titlebar_api_remembers_and_applies_the_theme(tmp_path):
    painted = []
    api = desktop.TitleBarApi(str(tmp_path / "titlebar.json"),
                              paint=lambda h, t: painted.append((h, t)) or True,
                              hwnd=lambda w: 7)
    assert api.saved_theme() is None and api.apply() is False      # nothing yet
    assert api.set_theme("dark") is False                          # no window yet
    assert api.saved_theme() == "dark"
    api.bind(object())
    assert api.apply() is True and painted[-1] == (7, "dark")      # remembered
    assert api.set_theme("light") is True and painted[-1] == (7, "light")
    assert api.set_theme("neon") is False
    (tmp_path / "titlebar.json").write_text("not json")
    assert api.saved_theme() is None
    (tmp_path / "titlebar.json").write_text('{"theme": "neon"}')
    assert api.saved_theme() is None


def test_titlebar_api_still_paints_when_the_state_file_cannot_be_written(tmp_path):
    api = desktop.TitleBarApi(str(tmp_path / "missing-dir" / "t.json"),
                              paint=lambda h, t: True, hwnd=lambda w: 7)
    api.bind(object())
    assert api.set_theme("dark") is True


def test_main_exposes_the_titlebar_api_and_paints_when_shown(tmp_path):
    class Event(list):
        def __iadd__(self, fn):
            self.append(fn)
            return self

    class WV(_WebView):
        def create_window(self, title, url=None, html=None, **kw):
            super().create_window(title, url, html, **kw)
            self.win = types.SimpleNamespace(destroy=lambda: None,
                                             events=types.SimpleNamespace(shown=Event()))
            return self.win

    wv = WV()
    desktop.main(env={"SYSPERF_DATA_DIR": str(tmp_path)}, webview=wv,
                 probe=lambda p: True, watch_update=lambda cb: None)
    api = wv.windows[0]["js_api"]
    assert isinstance(api, desktop.TitleBarApi) and api._window is wv.win
    assert api._path == str(tmp_path / "webview" / "titlebar.json")
    assert len(wv.win.events.shown) == 1
    wv.win.events.shown[0]()          # nothing remembered yet: a harmless no-op
