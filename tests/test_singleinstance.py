"""5.5.3: one SysPerf per port (singleinstance.py)."""
import socket

from sysperf import singleinstance


def test_second_claim_of_the_same_port_is_refused():
    first = singleinstance.claim_port("127.0.0.1", 0)
    assert first is not None
    port = first.getsockname()[1]
    try:
        assert singleinstance.claim_port("127.0.0.1", port) is None
    finally:
        first.close()
    again = singleinstance.claim_port("127.0.0.1", port)      # free once released
    assert again is not None
    again.close()


class _Sock:
    def __init__(self, fail=False):
        self.opts, self.closed, self.fail = [], False, fail

    def setsockopt(self, *a):
        self.opts.append(a)

    def bind(self, addr):
        if self.fail:
            raise OSError("in use")
        self.addr = addr

    def close(self):
        self.closed = True


def test_windows_bind_is_exclusive_and_failure_closes_the_socket():
    made = []

    def factory(*a):
        made.append(_Sock())
        return made[-1]

    s = singleinstance.claim_port("127.0.0.1", 5056, windows=True, sock_factory=factory)
    assert s is made[0] and s.addr == ("127.0.0.1", 5056)
    excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", -5)
    assert s.opts == [(socket.SOL_SOCKET, excl, 1)]

    bad = _Sock(fail=True)
    assert singleinstance.claim_port("127.0.0.1", 5056, windows=False,
                                     sock_factory=lambda *a: bad) is None
    assert bad.closed and bad.opts == []
