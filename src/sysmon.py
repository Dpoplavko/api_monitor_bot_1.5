"""
sysmon: helpers for server status (AWS metadata + system metrics).

This module is optional; it tries to be robust in non-AWS environments.
"""
from __future__ import annotations

import os
import socket
import time
from typing import Any, Dict, Optional, List
import logging
from collections import deque

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None  # type: ignore


AWS_IMDS_V2_TOKEN_URL = "http://169.254.169.254/latest/api/token"
AWS_IMDS_BASE = "http://169.254.169.254/latest/meta-data"

_bot_start_ts: float = time.time()
_ERRORS: deque[Dict[str, Any]] = deque(maxlen=50)

class _MemoryErrorHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # type: ignore[override]
        try:
            if record.levelno >= logging.ERROR:
                _ERRORS.append({
                    "ts": int(getattr(record, 'created', time.time())),
                    "level": record.levelname,
                    "name": record.name,
                    "msg": self.format(record),
                })
        except Exception:
            pass

def install_log_capture(logger_name: Optional[str] = None) -> None:
    """Install a memory handler to capture recent ERRORs. Idempotent."""
    logger = logging.getLogger(logger_name) if logger_name else logging.getLogger()
    # Avoid duplicate handlers
    for h in logger.handlers:
        if isinstance(h, _MemoryErrorHandler):
            return
    h = _MemoryErrorHandler(level=logging.ERROR)
    fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s')
    h.setFormatter(fmt)
    logger.addHandler(h)

def get_recent_errors(limit: int = 5) -> List[Dict[str, Any]]:
    return list(_ERRORS)[-limit:]

def set_bot_start(ts: Optional[float] = None) -> None:
    global _bot_start_ts
    _bot_start_ts = float(ts or time.time())

def _format_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts: List[str] = []
    if d: parts.append(f"{d}д")
    if h: parts.append(f"{h}г")
    if m: parts.append(f"{m}хв")
    if not parts: parts.append(f"{s}с")
    return " ".join(parts)


async def _aws_imds_headers(client: Any) -> Dict[str, str]:
    """Get IMDSv2 token if possible; fall back to none."""
    if httpx is None:
        return {}
    try:
        r = await client.put(AWS_IMDS_V2_TOKEN_URL, headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}, timeout=1.0)
        if r.status_code == 200:
            return {"X-aws-ec2-metadata-token": r.text}
    except Exception:
        pass
    return {}


async def get_aws_metadata() -> Dict[str, Any]:
    """Fetch basic AWS instance metadata; returns {} if not available."""
    if httpx is None:
        return {}
    meta: Dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:  # type: ignore
            headers = await _aws_imds_headers(client)
            async def g(path: str) -> Optional[str]:
                try:
                    r = await client.get(f"{AWS_IMDS_BASE}/{path}", headers=headers)
                    return r.text.strip() if r.status_code == 200 else None
                except Exception:
                    return None
            meta["instance-id"] = await g("instance-id")
            meta["instance-type"] = await g("instance-type")
            meta["availability-zone"] = await g("placement/availability-zone")
            meta["local-ipv4"] = await g("local-ipv4")
            meta["public-ipv4"] = await g("public-ipv4")
    except Exception:
        return {}
    # Drop empty keys
    return {k: v for k, v in meta.items() if v}


def get_system_metrics() -> Dict[str, Any]:
    """Return basic system metrics using psutil when available."""
    out: Dict[str, Any] = {
        "hostname": socket.gethostname(),
        "time": int(time.time()),
    }
    if psutil is None:
        return out
    try:
        out["cpu_percent"] = psutil.cpu_percent(interval=0.2)
        vm = psutil.virtual_memory()
        out["mem_total_mb"] = int(vm.total / (1024*1024))
        out["mem_used_mb"] = int(vm.used / (1024*1024))
        out["mem_percent"] = float(vm.percent)
        du = psutil.disk_usage("/")
        out["disk_total_gb"] = round(du.total / (1024**3), 2)
        out["disk_used_gb"] = round(du.used / (1024**3), 2)
        out["disk_percent"] = float(du.percent)
        net = psutil.net_io_counters()
        out["net_bytes_sent"] = int(getattr(net, 'bytes_sent', 0) or 0)
        out["net_bytes_recv"] = int(getattr(net, 'bytes_recv', 0) or 0)
        try:
            temps: dict[str, list[Any]] = {}
            fn = getattr(psutil, 'sensors_temperatures', None)
            if callable(fn):
                res = fn()
                if isinstance(res, dict):
                    temps = res  # type: ignore[assignment]
            if temps:
                k = next(iter(temps.keys()))
                arr: list[Any] = list(temps.get(k) or [])
                if arr:
                    cur = getattr(arr[0], 'current', None)
                    if isinstance(cur, (int, float)):
                        out["cpu_temp_c"] = float(cur)
        except Exception:
            pass
        try:
            load1, load5, load15 = os.getloadavg()  # type: ignore
            out["loadavg"] = {"1m": load1, "5m": load5, "15m": load15}
        except Exception:
            pass
    except Exception:
        pass
    return out


async def format_server_status(bot_health: Optional[str] = None) -> str:
    """Build a human-readable server status string."""
    aws = await get_aws_metadata()
    sysm = get_system_metrics()
    lines = ["🖥️ <b>Статус сервера</b>"]
    if aws:
        lines.append(
            "AWS: " + ", ".join([
                f"id {aws.get('instance-id')}",
                f"type {aws.get('instance-type')}",
                f"az {aws.get('availability-zone')}",
                f"ip {aws.get('local-ipv4')} / {aws.get('public-ipv4', '—')}"
            ])
        )
    lines.append(
        "CPU: {cpu}% | RAM: {used}/{total} MB ({ramp}%) | Disk: {du}/{dt} GB ({dp}%)".format(
            cpu=int(sysm.get("cpu_percent", 0)),
            used=int(sysm.get("mem_used_mb", 0)),
            total=int(sysm.get("mem_total_mb", 0)),
            ramp=float(sysm.get("mem_percent", 0.0)),
            du=float(sysm.get("disk_used_gb", 0.0)),
            dt=float(sysm.get("disk_total_gb", 0.0)),
            dp=float(sysm.get("disk_percent", 0.0)),
        )
    )
    if "loadavg" in sysm:
        la = sysm["loadavg"]
        lines.append(f"LoadAvg: {la.get('1m'):.2f} {la.get('5m'):.2f} {la.get('15m'):.2f}")
    # Bot uptime
    try:
        uptime = _format_duration(time.time() - _bot_start_ts)
        lines.append(f"Uptime бота: {uptime}")
    except Exception:
        pass
    if bot_health:
        lines.append(bot_health)
    # Recent errors
    errs = get_recent_errors(5)
    if errs:
        lines.append("\n<b>Останні помилки:</b>")
        for e in errs:
            ts = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(e.get('ts', int(time.time()))))
            lvl = e.get('level', 'ERROR')
            name = e.get('name', '-')
            msg = str(e.get('msg', ''))
            # trim long lines
            if len(msg) > 200:
                msg = msg[:200] + '…'
            lines.append(f"• {ts} UTC [{lvl}] {name}: {msg}")
    return "\n".join(lines)
