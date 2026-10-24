"""One settings surface for everything that used to live in four places.

Before 4.1 settings were split across ``sysperf.config.json`` (written by the
installer, partly ignored), ``evolution_settings.json``, ``autonomy.json``,
``trayquit_state.json`` and a handful of dashboard controls. The hub puts each
behind a :class:`Section` with a declared schema, so the page can render one
form, the server validates every value against the same schema, and the whole
set can be exported, imported or reset.

A section does not own its storage: it delegates to the component that already
does (the action policy, the evolution engine, the tray-quit watcher...), so
there is still exactly one writer per setting and every existing guard —
range checks, protected keys, audit records — keeps applying.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class Field:
    key: str
    label: str
    type: str                        # bool | int | float | choice | list
    help: str = ""
    min: float | None = None
    max: float | None = None
    choices: list = field(default_factory=list)
    restart: bool = False            # takes effect after the service restarts

    def validate(self, value):
        if self.type == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{self.label}: must be true or false")
            return value
        if self.type in ("int", "float"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{self.label}: must be a number")
            if self.type == "int" and int(value) != value:
                raise ValueError(f"{self.label}: must be a whole number")
            if (self.min is not None and value < self.min) or \
                    (self.max is not None and value > self.max):
                raise ValueError(f"{self.label}: must be between {self.min:g} and {self.max:g}")
            return int(value) if self.type == "int" else float(value)
        if self.type == "choice":
            if value not in self.choices:
                raise ValueError(f"{self.label}: must be one of {', '.join(map(str, self.choices))}")
            return value
        if self.type == "list":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"{self.label}: must be a list of names")
            return [v.strip() for v in value if v.strip()]
        raise ValueError(f"{self.label}: unknown field type {self.type}")   # pragma: no cover


class Section:
    def __init__(self, name: str, label: str, fields: list, get: Callable[[], dict],
                 apply: Callable[[dict], None], defaults: dict):
        self.name, self.label, self.fields = name, label, fields
        self._get, self._apply, self.defaults = get, apply, defaults
        self.by_key = {f.key: f for f in fields}

    def validate(self, values: dict) -> dict:
        if not isinstance(values, dict):
            raise ValueError(f"{self.label}: expected an object")
        clean = {}
        for k, v in values.items():
            if k not in self.by_key:
                raise ValueError(f"{self.label}: '{k}' is not a setting here")
            clean[k] = self.by_key[k].validate(v)
        return clean

    def values(self) -> dict:
        cur = self._get()
        return {f.key: cur.get(f.key, self.defaults.get(f.key)) for f in self.fields}

    def describe(self) -> dict:
        return {"label": self.label, "values": self.values(), "defaults": dict(self.defaults),
                "fields": [asdict(f) for f in self.fields]}


class SettingsHub:
    def __init__(self, sections: list, on_change: Callable[[dict], None] | None = None):
        self.sections = {s.name: s for s in sections}
        self.on_change = on_change
        self.pending_restart: set = set()

    def describe(self) -> dict:
        # "order" because JSON object key order is not preserved (Flask sorts keys).
        return {"sections": {n: s.describe() for n, s in self.sections.items()},
                "order": list(self.sections), "restart_required": sorted(self.pending_restart)}

    def export(self) -> dict:
        return {"sysperf_settings": 1,
                "values": {n: s.values() for n, s in self.sections.items()}}

    def apply(self, values: dict) -> dict:
        """Validate every section first; apply only if all of them are valid."""
        if not isinstance(values, dict):
            raise ValueError("settings must be an object of sections")
        cleaned = {}
        for name, vals in values.items():
            if name not in self.sections:
                raise ValueError(f"unknown settings section '{name}'")
            cleaned[name] = self.sections[name].validate(vals)
        changed = {}
        for name, clean in cleaned.items():
            sec = self.sections[name]
            before = sec.values()
            diff = {k: v for k, v in clean.items() if before.get(k) != v}
            if not diff:
                continue
            sec._apply(diff)
            changed[name] = diff
            for k in diff:
                if sec.by_key[k].restart:
                    self.pending_restart.add(f"{name}.{k}")
        if changed and self.on_change is not None:
            self.on_change(changed)
        return {"changed": changed, **self.describe()}

    def reset(self, section: str | None = None) -> dict:
        names = [section] if section else list(self.sections)
        for n in names:
            if n not in self.sections:
                raise ValueError(f"unknown settings section '{n}'")
        return self.apply({n: dict(self.sections[n].defaults) for n in names})

    def import_(self, doc: dict) -> dict:
        if not isinstance(doc, dict) or doc.get("sysperf_settings") != 1 \
                or not isinstance(doc.get("values"), dict):
            raise ValueError("not a SysPerf settings export")
        return self.apply(doc["values"])


class ConfigFile:
    """Read/write keys of ``sysperf.config.json`` addressed by dotted paths,
    preserving everything else in the file (driver notes, Wi-Fi plans...)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, dotted: str, default=None):
        node = self.load()
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set_many(self, changes: dict) -> None:
        data = self.load()
        for dotted, value in changes.items():
            node = data
            parts = dotted.split(".")
            for part in parts[:-1]:
                if not isinstance(node.get(part), dict):
                    node[part] = {}
                node = node[part]
            node[parts[-1]] = value
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=4), encoding="utf-8")
        tmp.replace(self.path)
