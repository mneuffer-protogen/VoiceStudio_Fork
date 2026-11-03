"""Learned duration estimates for queued and running work.

Every finished job is a sample: its features (kind of work, engine, quality
steps, voice, device) and its size (characters of text, 0 when not
applicable) against how long it actually took. Samples update a small
hierarchy of linear models ``seconds = base + per_char * chars``:

    kind → kind+device → kind+device+engine → …+steps → …+voice

A prediction starts at the most general level and blends in each more
specific level by how much evidence it has (``n / (n + PRIOR_WEIGHT)``), so a
voice seen once barely moves the estimate while a voice seen twenty times
dominates it. Older samples decay (``DECAY``) so the model follows the
hardware it runs on. State is a few KB of JSON in the data directory and
survives restarts and redeploys on the same volume.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping

logger = logging.getLogger("omnivoice.estimates")

#: Weight kept by old evidence on every new sample at the same node.
DECAY = 0.97
#: Pseudo-count a node needs before it outweighs its parent.
PRIOR_WEIGHT = 3.0
#: Feature order, general → specific.
LEVELS = ("device", "engine", "steps", "voice")
#: Cold-start guess before any sample exists (seconds, seconds/char).
DEFAULT_BASE_S = 20.0
DEFAULT_PER_CHAR_S = 0.25
_MAX_NODES = 5000

_lock = threading.Lock()
_nodes: dict[str, dict] | None = None


def _path() -> str:
    from core.config import DATA_DIR

    return os.path.join(DATA_DIR, "queue_estimates.json")


def _load() -> dict[str, dict]:
    global _nodes
    if _nodes is None:
        try:
            with open(_path(), encoding="utf-8") as handle:
                data = json.load(handle)
            _nodes = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _nodes = {}
    return _nodes


def _save(nodes: dict[str, dict]) -> None:
    path = _path()
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(nodes, handle, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        logger.debug("could not persist estimates", exc_info=True)


def _keys(features: Mapping[str, object]) -> list[str]:
    """Node keys for these features, general → specific."""
    kind = str(features.get("kind") or "work")
    keys = [kind]
    for level in LEVELS:
        value = features.get(level)
        if value in (None, ""):
            continue
        keys.append(f"{keys[-1]}|{level}={value}")
    return keys


def _shape(chars: float) -> float:
    return DEFAULT_BASE_S + DEFAULT_PER_CHAR_S * chars


def _local(node: dict, chars: float) -> float:
    """This node's own prediction: weighted least squares ``a + b*chars``.

    With no spread in size yet (every sample about as long), the node only
    knows its typical time, so that is scaled by the default size curve."""
    w, sx, sy, sxx, sxy = (node[k] for k in ("w", "sx", "sy", "sxx", "sxy"))
    mx, my = sx / w, sy / w
    var = sxx / w - mx * mx
    if var > 25.0:  # at least ~5 characters of spread
        b = max(0.0, (sxy / w - mx * my) / var)
        a = my - b * mx
        if a < 0:  # keep the intercept physical: refit through the origin
            a, b = 0.0, sxy / sxx
        return a + b * chars
    return my * _shape(chars) / _shape(mx)


def estimate(features: Mapping[str, object]) -> dict:
    """``{"seconds": float, "samples": int}`` for a job with these features.

    ``samples`` counts the finished jobs at the most specific level that has
    any, so the UI can say how well-founded the number is."""
    chars = max(0.0, float(features.get("chars") or 0))
    with _lock:
        nodes = _load()
        prediction = _shape(chars)
        samples = 0
        for key in _keys(features):
            node = nodes.get(key)
            if not node or node.get("w", 0) <= 0:
                break
            weight = node["w"] / (node["w"] + PRIOR_WEIGHT)
            prediction = weight * _local(node, chars) + (1 - weight) * prediction
            samples = int(node.get("n", 0))
    return {"seconds": round(max(1.0, prediction), 1), "samples": samples}


def record(features: Mapping[str, object], seconds: float) -> None:
    """Add one finished job (``seconds`` of execution) to every level."""
    if not seconds or seconds <= 0:
        return
    x = max(0.0, float(features.get("chars") or 0))
    y = float(seconds)
    with _lock:
        nodes = _load()
        for key in _keys(features):
            node = nodes.get(key)
            if node is None:
                if len(nodes) >= _MAX_NODES:
                    break
                node = nodes[key] = {"w": 0.0, "sx": 0.0, "sy": 0.0, "sxx": 0.0, "sxy": 0.0, "n": 0}
            for field in ("w", "sx", "sy", "sxx", "sxy"):
                node[field] *= DECAY
            node["w"] += 1.0
            node["sx"] += x
            node["sy"] += y
            node["sxx"] += x * x
            node["sxy"] += x * y
            node["n"] = int(node.get("n", 0)) + 1
        _save(nodes)


def reset_for_tests(nodes: dict | None = None) -> None:
    global _nodes
    with _lock:
        _nodes = {} if nodes is None else nodes
