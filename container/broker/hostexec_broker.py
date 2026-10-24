#!/usr/bin/env python3
"""
SysPerf host-exec broker.

SysPerf measures and remediates a *Windows host*. Every sample is a
``powershell.exe`` invocation reaching out of WSL through interop, and the
component panel probes WSL-side binaries. A container has neither: no
``/mnt/c``, and no ``WSLInterop`` handler in its own ``binfmt_misc`` instance,
so a Windows ``.exe`` cannot be executed inside it at all.

The usual answer is ``--privileged``, which was measured to work but hands the
container effective root on the host and leaves it *more* WSL-coupled than
before. This broker is the alternative: the single place where host execution
happens. It runs on the WSL host as the ordinary user, listens on a unix
socket, and executes only argv vectors whose program is on an allowlist. The
container gets the socket and nothing else -- no capabilities, no ``/mnt/c``,
no interop, default seccomp, read-only root filesystem.

Every request is logged. That is the point of funnelling execution through one
process: the host surface is auditable instead of implicit.

Protocol (one request per connection, newline-delimited JSON):

    -> {"argv": ["powershell.exe", "-File", "/app/collect_win.ps1"],
        "timeout": 60, "cwd": null}
    <- {"rc": 0, "stdout": "...", "stderr": "", "error": null}
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
POLICY_FILE = Path(os.environ.get("SYSPERF_BROKER_POLICY", HERE / "policy.json"))
SOCKET_PATH = Path(os.environ.get("SYSPERF_BROKER_SOCKET",
                                  "/run/sysperf-hostexec/sock"))
LOG_FILE = Path(os.environ.get("SYSPERF_BROKER_LOG", HERE / "broker.log"))

MAX_REQUEST_BYTES = 4 << 20      # the frontend gate pipes a ~150 KB script block
MAX_TIMEOUT_S = 300.0
MAX_OUTPUT_BYTES = 8 << 20       # the -Full inventory sample is ~1-2 MiB

log = logging.getLogger("hostexec")


# --------------------------------------------------------------------- policy --
class Policy:
    """Allowlist and path translation, loaded once at start-up.

    Deliberately not reloaded per request: a policy that can change underneath
    a running broker is a policy you cannot reason about from the log.
    """

    def __init__(self, raw: dict) -> None:
        self.allow: set[str] = set(raw.get("allow", []))
        self.program_paths: dict[str, str] = dict(raw.get("program_paths", {}))
        self.path_map: list[tuple[str, str]] = sorted(
            ((k.rstrip("/") + "/", v.rstrip("/") + "/")
             for k, v in raw.get("path_map", {}).items()),
            key=lambda kv: -len(kv[0]))          # longest prefix wins
        self.script_roots: list[Path] = [Path(p).resolve()
                                         for p in raw.get("script_roots", [])]
        self.allow_arbitrary_pwsh: bool = bool(raw.get("allow_arbitrary_pwsh", True))
        self.powershell: str | None = raw.get("powershell") or _find_powershell()

    @classmethod
    def load(cls, path: Path) -> "Policy":
        return cls(json.loads(path.read_text()))

    # -- path translation ---------------------------------------------------
    def host_path(self, text: str) -> str:
        """Rewrite a container-visible path to its host equivalent.

        The container sees the code at ``/app`` and its state at ``/data``;
        PowerShell on the Windows side has to be handed something that exists
        on the host. Without this, ``-File /app/collect_win.ps1`` would be
        passed through verbatim and every sample would fail with a
        file-not-found that looks like a collector bug.
        """
        for prefix, replacement in self.path_map:
            if text.startswith(prefix):
                return replacement + text[len(prefix):]
            if text == prefix.rstrip("/"):
                return replacement.rstrip("/")
        return text

    # -- program resolution -------------------------------------------------
    def resolve(self, program: str) -> tuple[str | None, str | None]:
        """(resolved absolute program, rejection reason)."""
        name = os.path.basename(program)
        if name not in self.allow:
            return None, f"program not allowed: {name}"
        if name in ("powershell.exe", "pwsh.exe"):
            if not self.powershell:
                return None, "no PowerShell found on this host"
            return self.powershell, None
        # An explicit absolute path from the policy. Needed for Windows
        # executables: the broker runs under systemd with a minimal PATH, so
        # `which wsl.exe` fails even though an interactive shell resolves it
        # through the /mnt/c interop entries. Without this, `wsl --version` on
        # the components panel was denied as "not found on host".
        pinned = self.program_paths.get(name)
        if pinned and os.path.exists(pinned):
            return pinned, None
        # An absolute path that survived translation and exists is used as-is.
        translated = self.host_path(program)
        if os.path.isabs(translated) and os.path.exists(translated):
            return translated, None
        found = shutil.which(name, path=_probe_path())
        if found:
            return found, None
        if name.lower().endswith(".exe"):
            for root in ("/mnt/c/Windows/System32", "/mnt/c/WINDOWS/system32"):
                candidate = os.path.join(root, name)
                if os.path.exists(candidate):
                    return candidate, None
        return None, f"not found on host: {name}"

    def check_script_args(self, argv: list[str]) -> str | None:
        """Reject a ``-File`` outside the known roots; allow ``-Command`` by policy.

        ``fixes.py`` builds ``[PS_EXE, '-Command', <remediation>]`` with a
        command string assembled at runtime, so an allowlist of command *content*
        would break the remediation feature it exists to serve. The honest
        position is that ``-Command`` passthrough is arbitrary execution on the
        Windows host, that it is logged, and that it can be switched off here.
        """
        lowered = [a.lower() for a in argv]
        for flag in ("-file", "-f"):
            if flag in lowered:
                idx = lowered.index(flag)
                if idx + 1 >= len(argv):
                    return f"{flag} with no argument"
                target = _as_linux_path(argv[idx + 1])
                if target is None:
                    return f"cannot resolve script path: {argv[idx + 1]}"
                if not any(_is_within(target, root) for root in self.script_roots):
                    return f"script outside allowed roots: {target}"
        if not self.allow_arbitrary_pwsh and "-command" in lowered:
            return "-Command is disabled by policy"
        return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _as_linux_path(text: str) -> Path | None:
    """Resolve a script argument to a Linux path for the root check.

    The collector hands PowerShell a *Windows* path -- ``to_host_path`` has
    already run it through ``wslpath -w``, so what arrives here looks like
    ``\\\\wsl.localhost\\<distro>\\<sysperf>\\src\\sysperf\\collect_win.ps1``.
    Comparing that against a POSIX root would reject every legitimate sample,
    so it is translated back before the containment test.
    """
    if "\\" not in text:
        return Path(text).resolve()
    try:
        res = subprocess.run(["wslpath", "-u", text], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (res.stdout or "").strip()
    if res.returncode != 0 or not out:
        return None
    return Path(out).resolve()


def _probe_path() -> str:
    """PATH used to resolve probe binaries.

    Mirrors the comment in ``components.py``: the user-local bins come first so
    a reported version matches the binary the user actually runs, not an older
    system-wide copy.
    """
    home = os.path.expanduser("~")
    return os.pathsep.join([f"{home}/.local/bin", f"{home}/bin",
                            os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")])


def _find_powershell() -> str | None:
    for candidate in ("/mnt/c/Program Files/PowerShell/7/pwsh.exe",
                      "/mnt/c/Program Files (x86)/PowerShell/7/pwsh.exe",
                      "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
                      "/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe"):
        if os.path.exists(candidate):
            return candidate
    return None


# -------------------------------------------------------------------- server --
class Handler(socketserver.StreamRequestHandler):
    timeout = MAX_TIMEOUT_S + 30

    def handle(self) -> None:                     # noqa: C901 - one linear path
        policy: Policy = self.server.policy       # type: ignore[attr-defined]
        try:
            line = self.rfile.readline(MAX_REQUEST_BYTES)
        except OSError as exc:
            log.warning("read failed: %s", exc)
            return
        if not line:
            return
        try:
            req = json.loads(line)
            argv = [str(a) for a in req["argv"]]
            if not argv:
                raise ValueError("empty argv")
        except (ValueError, KeyError, TypeError) as exc:
            self._reply({"rc": 126, "stdout": "", "stderr": "",
                         "error": f"bad request: {exc}"})
            return

        program, reason = policy.resolve(argv[0])
        if reason:
            log.warning("DENY %s -- %s", argv[:3], reason)
            self._reply({"rc": 126, "stdout": "", "stderr": "", "error": reason})
            return

        translated = [program] + [policy.host_path(a) for a in argv[1:]]
        reason = policy.check_script_args(translated)
        if reason:
            log.warning("DENY %s -- %s", argv[:3], reason)
            self._reply({"rc": 126, "stdout": "", "stderr": "", "error": reason})
            return

        timeout = min(float(req.get("timeout") or 60.0), MAX_TIMEOUT_S)
        cwd = req.get("cwd")
        cwd = policy.host_path(str(cwd)) if cwd else None
        if cwd and not os.path.isdir(cwd):
            cwd = None

        log.info("EXEC %s", _summarise(translated))
        # The test suite's frontend gate pipes an inline <script> block to
        # `node --check -`, so stdin has to survive the hop or that gate fails
        # with an empty-input syntax error rather than a real result.
        stdin_text = req.get("stdin")
        stdin_text = str(stdin_text) if stdin_text is not None else None
        try:
            res = subprocess.run(translated, capture_output=True, text=True,
                                 timeout=timeout, cwd=cwd, input=stdin_text,
                                 env={**os.environ, "PATH": _probe_path()})
        except subprocess.TimeoutExpired:
            log.warning("TIMEOUT after %ss: %s", timeout, _summarise(translated))
            self._reply({"rc": 124, "stdout": "", "stderr": "",
                         "error": f"timed out after {timeout}s"})
            return
        except OSError as exc:
            log.warning("EXEC failed: %s -- %s", _summarise(translated), exc)
            self._reply({"rc": 127, "stdout": "", "stderr": "", "error": str(exc)})
            return

        self._reply({"rc": res.returncode,
                     "stdout": (res.stdout or "")[:MAX_OUTPUT_BYTES],
                     "stderr": (res.stderr or "")[:MAX_OUTPUT_BYTES],
                     "error": None})

    def _reply(self, payload: dict) -> None:
        try:
            self.wfile.write((json.dumps(payload) + "\n").encode())
            self.wfile.flush()
        except OSError:
            pass                                  # client gave up; nothing to do


def _summarise(argv: list[str], limit: int = 240) -> str:
    text = " ".join(argv)
    return text if len(text) <= limit else text[:limit] + f"... (+{len(text) - limit})"


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, policy: Policy) -> None:
        self.policy = policy
        super().__init__(path, Handler)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr),
                  logging.FileHandler(LOG_FILE)])

    try:
        policy = Policy.load(POLICY_FILE)
    except (OSError, ValueError) as exc:
        log.error("cannot load policy %s: %s", POLICY_FILE, exc)
        return 2

    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists() or SOCKET_PATH.is_socket():
        SOCKET_PATH.unlink(missing_ok=True)       # a stale socket blocks bind()

    server = Server(str(SOCKET_PATH), policy)
    # 0660 and not 0666: the container runs as this same uid, so group/owner
    # access is sufficient and nothing else on the host needs to reach it.
    os.chmod(SOCKET_PATH, 0o660)
    log.info("listening on %s | powershell=%s | allow=%s",
             SOCKET_PATH, policy.powershell, sorted(policy.allow))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()
        SOCKET_PATH.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
