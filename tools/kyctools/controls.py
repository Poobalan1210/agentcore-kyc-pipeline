"""Runtime controls, read on every call: demo fault flags and the screening list overlay.

Why per call and not at import. The Gateway invokes the published `live` alias of the tools
function (`tools_stack.py`), so an environment variable edited on the function never reaches it:
the alias still points at the version that was published with the old value. A fault switch that
needs a redeploy to flip cannot be flipped on stage. Both controls therefore live in SSM Parameter
Store and are re-read after a short cache interval.

The two controls fail in opposite directions, deliberately:

* **Fault flags fail open.** A switch that exists to *break* the demo must never break production
  when Parameter Store is unreachable. Unreadable flags mean "no faults".
* **The list overlay fails closed.** It carries sanctions list updates. Screening against a list
  that could not be loaded would be screening against a stale list, so an unreadable overlay makes
  the screening tool UNAVAILABLE — which routes to a human.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

from .contract import ToolError
from .data.sanctions import Entry

_CACHE: dict[str, tuple[float, Any]] = {}


def _ttl_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("CONTROL_TTL_SECONDS", "3")))
    except ValueError:
        return 3.0


def reset_cache() -> None:
    _CACHE.clear()


def _read_parameter(name: str) -> str:
    import boto3

    ssm = boto3.client("ssm")
    return ssm.get_parameter(Name=name)["Parameter"]["Value"]


def _cached(name: str, loader: Callable[[], Any]) -> Any:
    now = time.monotonic()
    hit = _CACHE.get(name)
    if hit is not None and now - hit[0] < _ttl_seconds():
        return hit[1]
    value = loader()
    _CACHE[name] = (now, value)
    return value


def demo_faults() -> dict[str, Any]:
    """`{"unavailableTools": [...], "agentTimeouts": {...}}`, or `{}`. Never raises."""
    name = os.environ.get("DEMO_FAULTS_PARAMETER")
    if not name:
        return {}
    try:
        raw = _cached(name, lambda: _read_parameter(name))
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001 - see the module docstring: fault flags fail open
        return {}


def unavailable_tools() -> set[str]:
    """Tools forced UNAVAILABLE, from the environment (tests, verify scripts) or the demo flags."""
    env = {t.strip() for t in os.environ.get("FORCE_UNAVAILABLE", "").split(",") if t.strip()}
    flagged = demo_faults().get("unavailableTools") or []
    if not isinstance(flagged, list):
        flagged = []
    return env | {str(t) for t in flagged}


def parse_overlay(raw: str | None) -> tuple[str | None, tuple[Entry, ...]]:
    """Parse the overlay document. Raises ValueError on anything malformed."""
    if not raw:
        return None, ()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("overlay must be a JSON object")
    version = data.get("version")
    entries_raw = data.get("entries") or []
    if not isinstance(entries_raw, list):
        raise ValueError("overlay entries must be a list")
    entries = []
    seen: set[str] = set()
    for i, e in enumerate(entries_raw):
        if not isinstance(e, dict):
            raise ValueError("overlay entry %d is not an object" % i)
        entry_id = str(e.get("entryId") or "").strip()
        name = str(e.get("name") or "").strip()
        if not entry_id or not name:
            raise ValueError("overlay entry %d needs entryId and name" % i)
        if entry_id in seen:
            raise ValueError("duplicate overlay entryId %s" % entry_id)
        seen.add(entry_id)
        dob = e.get("dob") or None
        aliases = e.get("aliases") or []
        entries.append(Entry(
            entry_id=entry_id, name=name,
            dob=str(dob) if dob else None,
            country=str(e.get("country") or "").upper(),
            programme=str(e.get("programme") or "SYN-UPDATE"),
            aliases=tuple(str(a) for a in aliases if a),
            note=str(e.get("note") or "published by a list update"),
        ))
    if entries and not version:
        raise ValueError("an overlay with entries must carry a version")
    return (str(version) if version else None), tuple(entries)


def list_overlay() -> tuple[str | None, tuple[Entry, ...]]:
    """The published list update, if one is configured. Raises `ToolError` if unreadable."""
    name = os.environ.get("LIST_OVERLAY_PARAMETER")
    if not name:
        return None, ()
    try:
        raw = _cached(name, lambda: _read_parameter(name))
        return parse_overlay(raw)
    except Exception as exc:  # noqa: BLE001 - see the module docstring: the overlay fails closed
        _CACHE.pop(name, None)
        raise ToolError("the screening list update could not be read (%s)" % type(exc).__name__,
                        "LIST_UNAVAILABLE") from exc
