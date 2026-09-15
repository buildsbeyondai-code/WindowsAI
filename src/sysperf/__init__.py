"""
SysPerf — System Performance Agent.

A read-only Windows 11 performance dashboard and advisor. It measures the
machine, learns its own baseline, scores progress against targets derived from
that machine's capacity, and recommends what to change — without ever changing
anything itself.

Public API::

    from sysperf.config import load_config
    from sysperf.app import create_app, serve
"""
from __future__ import annotations

from .config import APP_NAME, APP_TITLE, VERSION

__all__ = ["APP_NAME", "APP_TITLE", "VERSION", "__version__"]
__version__ = VERSION
