# src/utils.py
# Допоміжні функції (парсер команд, форматування повідомлень)

import datetime
import json
import io
from typing import Dict, Optional, List, Any, Tuple
from statistics import quantiles
from config import settings
# Avoid importing runtime_config here to prevent circular imports.
CHART_KEYS = {
    'CHART_STYLE','CHART_Y_SCALE','CHART_SHOW_UCL','CHART_SHOW_EWMA','CHART_EWMA_ALPHA',
    'CHART_SHOW_PERCENTILES','CHART_MARK_FAILURES','CHART_POINT_EVERY','CHART_MARK_ANOMALIES',
    'CHART_SHOW_RAW_LINE','CHART_AGGREGATION','CHART_AGG_PERCENTILE','CHART_LTTB_POINTS',
    'CHART_SIZE','CHART_DPI'
}

def _effective_chart_config(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    eff: Dict[str, Any] = {k: getattr(settings, k) for k in CHART_KEYS if hasattr(settings, k)}
    if overrides:
        eff.update(overrides)
    return eff

# Використовуємо Agg-бекенд для генерації графіків на сервері без GUI
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import ScalarFormatter, NullFormatter
import statistics
import math

# --- ML/stat helpers ---
def robust_stats(values: List[int]) -> Dict[str, float]:
    """Обчислює медіану, MAD, EWMA, P95 та верхню контрольну межу (UCL ~ медіана + 3*MAD).
    Повертає dict із median, mad, ewma, p95, ucl.
    """
    if not values:
        return {"median": 0.0, "mad": 0.0, "ewma": 0.0, "p95": 0.0, "ucl": 0.0}
    med = float(statistics.median(values))
    abs_dev = [abs(v - med) for v in values]
    mad = float(statistics.median(abs_dev)) or 0.0
    # EWMA з альфою 0.3 (помірно чутлива)
    alpha = 0.3
    ewma = float(values[0])
    for v in values[1:]:
        ewma = alpha * v + (1 - alpha) * ewma
    # UCL: медіана + 3 * 1.4826 * MAD (прибл. еквівалент std для нормального розподілу)
    ucl = med + 3.0 * 1.4826 * mad
    # P95
    try:
        vs = sorted(values)
        if vs:
            idx = min(len(vs)-1, max(0, int(math.ceil(0.95 * len(vs)) - 1)))
            p95 = float(vs[idx])
        else:
            p95 = 0.0
    except Exception:
        p95 = 0.0
    return {"median": med, "mad": mad, "ewma": ewma, "p95": p95, "ucl": ucl}

def detect_anomaly(value: int, ucl: float) -> Tuple[bool, float]:
    """Проста детекція: спрацьовує, якщо значення > UCL. score = (value - ucl)."""
    if ucl <= 0:
        return False, 0.0
    is_anom = value > ucl
    score = float(value - ucl) if is_anom else 0.0
    return is_anom, score

def parse_period_to_timedelta(period_str: str) -> Optional[datetime.datetime]:
    """Конвертує рядок періоду (1h, 7d) в об'єкт datetime."""
    now = datetime.datetime.utcnow()
    period_str = period_str.lower()
    
    try:
        if period_str.endswith('h'):
            hours = int(period_str[:-1])
            return now - datetime.timedelta(hours=hours)
        elif period_str.endswith('d'):
            days = int(period_str[:-1])
            return now - datetime.timedelta(days=days)
    except (ValueError, IndexError):
        return None
    return None

def format_timedelta(td: datetime.timedelta) -> str:
    """Форматує timedelta в читабельний рядок (2 год 15 хв 30 сек)."""
    total_seconds = int(td.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if days > 0: parts.append(f"{days} дн")
    if hours > 0: parts.append(f"{hours} год")
    if minutes > 0: parts.append(f"{minutes} хв")
    if seconds > 0 or not parts: parts.append(f"{seconds} сек")
        
    return " ".join(parts) if parts else "0 сек"

def get_period_text(period_str: str) -> str:
    period_map = {
        "1h": "останню годину", "6h": "останні 6 годин", "12h": "останні 12 годин",
        "24h": "останні 24 години", "7d": "останні 7 днів", "30d": "останні 30 днів"
    }
    return period_map.get(period_str, period_str)

async def generate_statistics_chart(history: List["CheckHistory"], api_name: str, period: str, avg_response_time: float, ucl_hint: Optional[float] = None, overrides: Optional[Dict[str, Any]] = None) -> io.BytesIO:
    """Генерує графік статистики та повертає його як байтовий буфер."""
    if not history:
        # Створюємо порожній графік з повідомленням, якщо немає даних
        fig, ax = plt.subplots(figsize=(12, 6.5))
        ax.text(0.5, 0.5, 'Немає даних для побудови графіка за цей період', 
                horizontalalignment='center', verticalalignment='center', 
                transform=ax.transAxes, fontsize=14, color='gray')
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100)
        buf.seek(0)
        plt.close(fig)
        return buf

    timestamps = [h.timestamp for h in history]
    response_times = [h.response_time_ms if (h.response_time_ms and h.response_time_ms > 0) else 1 for h in history]  # 0 -> 1 для лог шкали

    ok_checks = [(h.timestamp, h.response_time_ms) for h in history if h.is_ok and (h.response_time_ms or 0) > 0]
    fail_checks = [(h.timestamp, h.response_time_ms) for h in history if (not h.is_ok) and (h.response_time_ms or 0) > 0]

    # Ефективні налаштування (env + runtime overrides)
    eff = _effective_chart_config(overrides)

    # Стиль і розмір графіка з конфігурації
    try:
        if eff.get('CHART_STYLE'):
            plt.style.use(eff.get('CHART_STYLE'))
    except Exception:
        plt.style.use('seaborn-v0_8-darkgrid')
    try:
        w, h = (float(x) for x in (eff.get('CHART_SIZE') or "12x6.5").lower().split('x'))
    except Exception:
        w, h = 12.0, 6.5
    fig, ax = plt.subplots(figsize=(w, h))

    # Determine UCL (for anomaly highlighting later)
    ucl_value: Optional[float] = None
    try:
        if (ucl_hint is not None) and (ucl_hint > 0):
            ucl_value = float(ucl_hint)
        elif int(eff.get('CHART_SHOW_UCL', 1)):
            stats_local = robust_stats([int(rt) for rt in response_times if rt is not None])
            if stats_local.get("ucl", 0) > 0:
                ucl_value = stats_local["ucl"]
    except Exception:
        ucl_value = None

    # Aggregation and downsampling strategy (supports 'auto')
    agg_mode_cfg = (str(eff.get('CHART_AGGREGATION') or 'per_minute')).lower()
    agg_mode = agg_mode_cfg
    if agg_mode_cfg == 'auto':
        # Heuristic: <=24h -> per_minute, else lttb
        per = period.lower().strip()
        try:
            if per.endswith('h') and int(per[:-1]) <= 24:
                agg_mode = 'per_minute'
            else:
                agg_mode = 'lttb'
        except Exception:
            agg_mode = 'per_minute'

    # Helper: per-minute aggregation (median and P95 of successful checks)
    def aggregate_per_minute() -> Tuple[List[datetime.datetime], List[float], List[float]]:
        buckets: Dict[datetime.datetime, List[int]] = {}
        for h in history:
            if h.is_ok and (h.response_time_ms or 0) > 0:
                minute = h.timestamp.replace(second=0, microsecond=0)
                buckets.setdefault(minute, []).append(int(h.response_time_ms))
        xs: List[datetime.datetime] = []
        medians: List[float] = []
        p95s: List[float] = []
        for minute in sorted(buckets.keys()):
            vals = sorted(buckets[minute])
            if not vals:
                continue
            xs.append(minute)
            medians.append(float(statistics.median(vals)))
            if len(vals) == 1:
                p95s.append(float(vals[0]))
            else:
                # approximate P95 index
                idx = min(len(vals)-1, max(0, int(math.ceil(0.95 * len(vals)) - 1)))
                p95s.append(float(vals[idx]))
        return xs, medians, p95s

    # Helper: LTTB downsampling for raw points
    def lttb(x: List[datetime.datetime], y: List[float], threshold: int) -> Tuple[List[datetime.datetime], List[float]]:
        n = len(x)
        if threshold >= n or threshold <= 0:
            return x, y
        # convert to numeric for area computation
        xs = [mdates.date2num(ts) for ts in x]
        bucket_size = (n - 2) / (threshold - 2)
        a = 0  # first point index kept
        sampled_x = [x[0]]
        sampled_y = [y[0]]
        for i in range(1, threshold - 1):
            avg_range_start = int(math.floor((i - 1) * bucket_size) + 1)
            avg_range_end = int(math.floor(i * bucket_size) + 1)
            avg_range_end = min(avg_range_end, n)
            avg_x = sum(xs[avg_range_start:avg_range_end]) / (avg_range_end - avg_range_start or 1)
            avg_y = sum(y[avg_range_start:avg_range_end]) / (avg_range_end - avg_range_start or 1)

            range_offs = int(math.floor(i * bucket_size) + 1)
            range_to = int(math.floor((i + 1) * bucket_size) + 1)
            range_to = min(range_to, n)

            # Find point in this bucket that forms the largest triangle area with previous selected (a) and avg point
            max_area = -1.0
            next_a = a
            for j in range(range_offs, range_to):
                area = abs((xs[a] - avg_x) * (y[j] - y[a]) - (xs[a] - xs[j]) * (avg_y - y[a])) / 2.0
                if area > max_area:
                    max_area = area
                    next_a = j
            sampled_x.append(x[next_a])
            sampled_y.append(y[next_a])
            a = next_a
        sampled_x.append(x[-1])
        sampled_y.append(y[-1])
        return sampled_x, sampled_y

    # Draw according to aggregation mode
    subtitle_bits: List[str] = []
    if agg_mode == 'per_minute':
        xs, med, p95 = aggregate_per_minute()
        if xs:
            ax.plot(xs, med, color='dodgerblue', linewidth=1.8, alpha=0.9, label='Медіана (хв)')
            ax.plot(xs, p95, color='slateblue', linestyle='--', linewidth=1.2, alpha=0.9, label='P95 (хв)')
            subtitle_bits.append('агрегація по хвилинах')
    elif agg_mode == 'lttb':
        target = max(50, int(eff.get('CHART_LTTB_POINTS', 240) or 240))
        xs, ys = lttb(timestamps, response_times, target)
        ax.plot(xs, ys, color='dodgerblue', linewidth=1.6, alpha=0.85, label=f'LTTB (~{target} тчк)')
        subtitle_bits.append('LTTB даунсемплінг')
    else:
        # fall back to raw thin line
        ax.plot(timestamps, response_times, color='dodgerblue', linewidth=1.2, alpha=0.6, label='Час відповіді (мс)')

    # Optional raw line as background for context
    if int(eff.get('CHART_SHOW_RAW_LINE', 1)) and agg_mode != 'none':
        ax.plot(timestamps, response_times, color='steelblue', linewidth=0.8, alpha=0.25, label='Сирий ряд')

    # Failures (always show)
    if fail_checks:
        fail_ts, fail_rt = zip(*fail_checks)
        ax.scatter(fail_ts, fail_rt, color='crimson', label='Збій', s=58, zorder=5, marker='X', linewidths=1.0, edgecolors='white')

    # Highlight anomalies if configured and UCL known (always show)
    if int(eff.get('CHART_MARK_ANOMALIES', 1)) and ucl_value:
        anoms = [(h.timestamp, h.response_time_ms) for h in history if h.is_ok and (h.response_time_ms or 0) > ucl_value]
        if anoms:
            a_ts, a_rt = zip(*anoms)
            ax.scatter(a_ts, a_rt, color='orange', edgecolors='white', linewidths=0.7, label='Аномалія', s=70, zorder=6)
    
    # Plot average response time line
    if avg_response_time:
        ax.axhline(y=avg_response_time, color='darkorange', linestyle='--', linewidth=1.5, label=f'Середній час ({int(avg_response_time)} мс)')

    # EWMA лінія (конфігурована)
    if int(eff.get('CHART_SHOW_EWMA', 1)):
        try:
            alpha = float(eff.get('CHART_EWMA_ALPHA', 0.3) or 0.3)
            ew = float(response_times[0])
            ew_series = [ew]
            for v in response_times[1:]:
                ew = alpha * v + (1 - alpha) * ew
                ew_series.append(ew)
            ax.plot(timestamps, ew_series, color='rebeccapurple', linewidth=1.7, alpha=0.8, label=f'EWMA (α={alpha:g})')
        except Exception:
            pass

    # UCL лінія (від ML або за історією)
    if int(eff.get('CHART_SHOW_UCL', 1)) and ucl_value:
        try:
            ax.axhline(y=ucl_value, color='crimson', linestyle=':', linewidth=1.6, label=f'UCL (~{int(ucl_value)} мс)')
        except Exception:
            pass

    # Shade downtime intervals derived from history (consecutive is_ok=False)
    try:
        down_start = None
        for h in history:
            if not h.is_ok and down_start is None:
                down_start = h.timestamp
            if h.is_ok and down_start is not None:
                ax.axvspan(down_start, h.timestamp, color='lightcoral', alpha=0.12, linewidth=0)
                down_start = None
        # If ended in down state
        if down_start is not None:
            ax.axvspan(down_start, timestamps[-1], color='lightcoral', alpha=0.12, linewidth=0)
    except Exception:
        pass

    # Перцентилі
    try:
        pct_cfg = str(eff.get('CHART_SHOW_PERCENTILES') or "").strip()
        if pct_cfg:
            pts = [p.strip() for p in pct_cfg.split(',') if p.strip()]
            vals = sorted([int(rt) for rt in response_times if rt is not None])
            for p in pts:
                pi = int(p)
                if 0 < pi < 100 and vals:
                    # simple quantile via statistics.quantiles
                    q = quantiles(vals, n=100)[pi-1]
                    ax.axhline(y=q, color='gray', linestyle='--', linewidth=0.8, alpha=0.6, label=f'P{pi}≈{int(q)} мс')
    except Exception:
        pass

    # Formatting
    subtitle = get_period_text(period)
    if subtitle_bits:
        subtitle += " · " + ", ".join(subtitle_bits)
    ax.set_title(f"Статистика відповіді для '{api_name}'\n за {subtitle}", fontsize=16, pad=20)
    ax.set_xlabel("Час", fontsize=12)
    ax.set_ylabel("Час відповіді (мс)", fontsize=12)
    ax.legend(loc='upper left', frameon=True, framealpha=0.9)
    yscale = (str(eff.get('CHART_Y_SCALE') or 'log')).lower()
    if yscale == 'auto':
        # Якщо діапазон вузький — linear, інакше log
        try:
            mn, mx = min(response_times), max(response_times)
            ax.set_yscale('linear' if mx <= 0 or (mx - mn) < 100 else 'log')
        except Exception:
            ax.set_yscale('log')
    else:
        ax.set_yscale(yscale if yscale in {'log','linear'} else 'log')
    
    # --- ПОКРАЩЕННЯ ---
    # Використовуємо звичайні числа замість степенів для осі Y
    ax.yaxis.set_major_formatter(ScalarFormatter())
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.tick_params(axis='y', which='minor', bottom=False)
    # Додаємо більше ліній сітки для кращої читабельності
    ax.grid(True, which='major', linestyle='--', linewidth=0.5)
    ax.grid(True, which='minor', linestyle=':', linewidth=0.3)
    # Softer spines for a cleaner look
    for spine in ['top', 'right']:
        ax.spines[spine].set_visible(False)
    # --- КІНЕЦЬ ПОКРАЩЕНЬ ---

    # Format x-axis dates
    date_format = mdates.DateFormatter('%H:%M\n%d-%m')
    ax.xaxis.set_major_formatter(date_format)
    fig.autofmt_xdate(rotation=0, ha='center')

    fig.tight_layout(pad=2.0)
    
    # Save to buffer
    buf = io.BytesIO()
    dpi = int(eff.get('CHART_DPI', 120) or 120)
    plt.savefig(buf, format='png', dpi=dpi)
    buf.seek(0)
    plt.close(fig)
    
    return buf

def _safe_pct(x: float) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0

def generate_conclusion(stats: dict, ml: Optional[Dict[str, Any]], anom: Optional[Dict[str, Any]]) -> str:
    """Генерує короткий висновок зрозумілою мовою."""
    uptime = _safe_pct(stats.get('uptime_percent', 100.0))
    avg_rt = int(stats.get('avg_response_time_ms') or 0)
    an_cnt = int((anom or {}).get('count') or 0)
    ml_med = int((ml or {}).get('median_ms') or 0)
    ml_ew = int((ml or {}).get('ewma_ms') or 0)
    drift = ml_ew - ml_med
    # Просте дерево рішень
    if uptime < 98.0 or an_cnt > 10:
        return "Сервіс має суттєві проблеми стабільності. Рекомендуємо перевірити інфраструктуру та залежності."
    if uptime < 99.0 or an_cnt > 5 or drift > max(100, ml_med * 0.3):
        return "Помітні ознаки деградації продуктивності. Варто звернути увагу на навантаження та бази даних."
    if avg_rt > max(500, ml_med * 1.5) or an_cnt > 0:
        return "Періодично спостерігаються сплески часу відповіді. Ситуація під контролем, але варто моніторити."
    return "Стан стабільний: аптайм високий, продуктивність у нормі."

def format_statistics_report(api_name: str, stats: dict, ml: Optional[Dict[str, Any]] = None, anom: Optional[Dict[str, Any]] = None) -> str:
    """Форматує словник зі статистикою в повідомлення для Telegram. Додає ML-аналітику, якщо є."""
    period_text = get_period_text(stats['period'])

    report = (
        f"📊 <b>Статистика для '{api_name}' за {period_text}</b>\n\n"
        f"  - <b>Аптайм:</b> {stats['uptime_percent']:.2f}%\n"
        f"  - <b>Середній час відповіді:</b> {int(stats['avg_response_time_ms'] or 0)} мс\n"
        f"  - <b>Кількість падінь:</b> {stats['incident_count']}\n"
        f"  - <b>Загальний час простою:</b> {format_timedelta(stats['total_downtime'])}\n"
        f"  - <b>Середня тривалість падіння:</b> {format_timedelta(stats['avg_downtime'])}"
    )
    if anom:
        report += f"\n  - <b>Аномалій (ML):</b> {int(anom.get('count') or 0)}"
    if ml:
        report += (
            "\n\n🧠 <b>ML-аналітика</b>\n"
            f"  - Медіана: {int(ml.get('median_ms') or 0)} мс\n"
            f"  - MAD: {int(ml.get('mad_ms') or 0)} мс\n"
            f"  - EWMA: {int(ml.get('ewma_ms') or 0)} мс\n"
            f"  - UCL (~поріг): {int(ml.get('ucl_ms') or 0)} мс\n"
            f"  - Вікно: {int(ml.get('window') or 0)}"
        )
    # Висновок
    report += "\n\n<b>Висновок:</b> " + generate_conclusion(stats, ml, anom)
    return report

def parse_add_command(text: str) -> Dict:
    parts = text.split()
    if not parts or not parts[0].startswith(('http://', 'https://')):
        raise ValueError("Команда має починатися з URL (http:// або https://)")

    data = {
        "url": parts[0], "method": "GET", "expected_status": 200, "timeout": 10,
        "check_interval": 60, "json_keys": None, "headers": None, "request_body": None,
    }
    
    if len(parts) > 1:
        data["method"] = parts[1].upper()
        if data["method"] not in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
             raise ValueError(f"Непідтримуваний метод: {data['method']}")
    if len(parts) > 2: data["expected_status"] = int(parts[2])
    if len(parts) > 3: data["timeout"] = int(parts[3])
    if len(parts) > 4: data["check_interval"] = int(parts[4])
    if len(parts) > 5 and parts[5].lower() != 'none': data["json_keys"] = parts[5]
        
    return data

def format_api_status(api: "MonitoredAPI", update_data: Optional[Dict] = None) -> str:
    is_up = update_data.get('is_up', api.is_up) if update_data else api.is_up
    last_error = update_data.get('last_error', api.last_error) if update_data else api.last_error
    
    status_icon = "🟢 <b>UP</b>" if is_up else "🔴 <b>DOWN</b>"
    active_icon = "▶️ Активний" if api.is_active else "⏸️ На паузі"

    text = (
        f"<b>Ім'я:</b> {api.name}\n"
        f"<b>ID:</b> <code>{api.id}</code>\n"
        f"<b>URL:</b> <code>{api.url}</code>\n"
        f"<b>Метод:</b> {api.method}\n"
        f"<b>Стан:</b> {status_icon}\n"
        f"<b>Моніторинг:</b> {active_icon}\n"
        f"------------------------------------\n"
        f"<b>Час відповіді:</b> {api.last_response_time or 'N/A'} мс\n"
        f"<b>Останній статус код:</b> {api.last_status_code or 'N/A'}\n"
        f"<b>Очікуваний статус:</b> {api.expected_status}\n"
        f"<b>Остання перевірка:</b> {api.last_checked.strftime('%Y-%m-%d %H:%M:%S UTC') if api.last_checked else 'N/A'}\n"
    )
    
    if api.headers:
        headers_str = json.dumps(api.headers, indent=2, ensure_ascii=False)
        text += f"<b>Заголовки:</b>\n<pre>{headers_str.replace('<', '&lt;')}</pre>\n"

    if api.request_body:
        body_str = json.dumps(api.request_body, indent=2, ensure_ascii=False)
        text += f"<b>Тіло запиту:</b>\n<pre>{body_str.replace('<', '&lt;')}</pre>\n"

    if not is_up and last_error:
        text += f"<b>Помилка:</b> <pre>{last_error.replace('<', '&lt;')}</pre>"
        
    return text