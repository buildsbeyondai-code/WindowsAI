"""Benchmarks for the evolution fixture. PROTECTED: candidates may not edit it."""
from dedupe import top_consumers, unique_in_order

_NAMES = [f"proc{i % 3000}" for i in range(30000)]
_SAMPLES = [{"name": f"p{i % 50}", "cpu": float(i % 7)} for i in range(20000)]

BENCHMARKS = {
    "unique_in_order": lambda: unique_in_order(_NAMES),
    "top_consumers": lambda: top_consumers(_SAMPLES),
}
