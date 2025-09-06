# src/runtime_config.py
# Runtime configuration helpers (persisted in DB) for chart settings.

from __future__ import annotations
from typing import Dict, Optional

from config import settings
from database import get_all_config, set_config_value

_CHART_KEYS = {
    "CHART_STYLE",
    "CHART_Y_SCALE",
    "CHART_SHOW_UCL",
    "CHART_SHOW_EWMA",
    "CHART_EWMA_ALPHA",
    "CHART_SHOW_PERCENTILES",
    "CHART_MARK_FAILURES",
    "CHART_POINT_EVERY",
    "CHART_MARK_ANOMALIES",
    "CHART_SHOW_RAW_LINE",
    "CHART_AGGREGATION",
    "CHART_AGG_PERCENTILE",
    "CHART_LTTB_POINTS",
    "CHART_SIZE",
    "CHART_DPI",
}

def _to_bool_like(v: str | int | float | None) -> int:
    if v is None:
        return 0
    s = str(v).strip().lower()
    return 1 if s in {"1","true","yes","y","on","t"} else 0

async def get_chart_overrides() -> Dict[str, object]:
    rows = await get_all_config()
    # Filter only known chart keys
    rows = {k: v for k, v in rows.items() if k in _CHART_KEYS and v is not None}
    out: Dict[str, object] = {}
    for k, v in rows.items():
        if k in {"CHART_SHOW_UCL","CHART_SHOW_EWMA","CHART_MARK_FAILURES","CHART_MARK_ANOMALIES","CHART_SHOW_RAW_LINE"}:
            out[k] = _to_bool_like(v)
        elif k in {"CHART_DPI","CHART_POINT_EVERY","CHART_LTTB_POINTS","CHART_AGG_PERCENTILE"}:
            try:
                out[k] = int(v)
            except Exception:
                continue
        elif k in {"CHART_EWMA_ALPHA"}:
            try:
                out[k] = float(v)
            except Exception:
                continue
        else:
            out[k] = v
    return out

async def set_chart_option(key: str, value: Optional[str]) -> None:
    if key not in _CHART_KEYS:
        raise ValueError("Невідома опція графіка")
    await set_config_value(key, None if value is None else str(value))

def get_effective_chart_config_sync(overrides: Dict[str, object] | None = None) -> Dict[str, object]:
    """Merge env settings with overrides; sync helper for code that doesn't await DB."""
    ov = overrides or {}
    eff = {k: getattr(settings, k) for k in _CHART_KEYS if hasattr(settings, k)}
    eff.update(ov)
    return eff
