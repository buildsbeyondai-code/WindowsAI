"""Process-name de-duplication used by the (fixture) sampler.

Deliberately inefficient: membership is tested against a list, so the
function is O(n^2). The self-evolution demo expects an LLM to find and fix it.
"""


def unique_in_order(names):
    """Return names with duplicates removed, first occurrence kept."""
    seen = []
    out = []
    for name in names:
        if name not in seen:
            seen.append(name)
            out.append(name)
    return out


def top_consumers(samples, n=5):
    """The n process names with the highest summed CPU, ties by name."""
    totals = {}
    for s in samples:
        totals[s["name"]] = totals.get(s["name"], 0.0) + s["cpu"]
    return [k for k, _ in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]
