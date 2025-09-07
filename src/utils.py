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
import plotly.graph_objects as go
import plotly.io as pio
from plotly.subplots import make_subplots
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
        # Порожнє полотно з повідомленням
        fig = go.Figure()
        fig.add_annotation(text='Немає даних для побудови графіка за цей період',
                           x=0.5, y=0.5, xref='paper', yref='paper', showarrow=False,
                           font=dict(size=16, color='gray'))
        fig.update_layout(width=900, height=520, margin=dict(l=50, r=30, t=60, b=60))
        buf = io.BytesIO()
        pio.write_image(fig, buf, format='png', scale=1)
        buf.seek(0)
        return buf

    timestamps = [h.timestamp for h in history]
    response_times = [h.response_time_ms if (h.response_time_ms and h.response_time_ms > 0) else 1 for h in history]
    ok_checks = [(h.timestamp, h.response_time_ms) for h in history if h.is_ok and (h.response_time_ms or 0) > 0]
    fail_checks = [(h.timestamp, h.response_time_ms) for h in history if (not h.is_ok) and (h.response_time_ms or 0) > 0]

    # Ефективні налаштування (env + runtime overrides)
    eff = _effective_chart_config(overrides)

    # Шаблон/тема для Plotly
    template = 'plotly_dark' if str(eff.get('CHART_STYLE') or '').lower().find('dark') >= 0 else 'plotly_white'
    try:
        w, h = (float(x) for x in (eff.get('CHART_SIZE') or "12x6.5").lower().split('x'))
    except Exception:
        w, h = 12.0, 6.5
    width_px = int(w * 75)
    height_px = int(h * 75)
    fig = go.Figure()

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
        # convert to numeric (epoch seconds) for area computation
        xs = [ts.timestamp() for ts in x]
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
            fig.add_trace(go.Scatter(x=xs, y=med, mode='lines', name='Медіана (хв)',
                                     line=dict(color='dodgerblue', width=1.8)))
            fig.add_trace(go.Scatter(x=xs, y=p95, mode='lines', name='P95 (хв)',
                                     line=dict(color='slateblue', width=1.2, dash='dash')))
            subtitle_bits.append('агрегація по хвилинах')
    elif agg_mode == 'lttb':
        target = max(50, int(eff.get('CHART_LTTB_POINTS', 240) or 240))
        xs, ys = lttb(timestamps, response_times, target)
        fig.add_trace(go.Scatter(x=xs, y=ys, mode='lines', name=f'LTTB (~{target} тчк)',
                                 line=dict(color='dodgerblue', width=1.6)))
        subtitle_bits.append('LTTB даунсемплінг')
    else:
        fig.add_trace(go.Scatter(x=timestamps, y=response_times, mode='lines', name='Час відповіді (мс)',
                                 line=dict(color='dodgerblue', width=1.2)))

    # Optional raw line as background for context
    if int(eff.get('CHART_SHOW_RAW_LINE', 1)) and agg_mode != 'none':
        fig.add_trace(go.Scatter(
            x=timestamps, y=response_times, mode='lines', name='Сирий ряд',
            line=dict(color='steelblue', width=0.8), opacity=0.25, showlegend=False
        ))

    # Failures (always show)
    if fail_checks:
        fail_ts, fail_rt = zip(*fail_checks)
        fig.add_trace(go.Scatter(x=list(fail_ts), y=list(fail_rt), mode='markers', name='Збій',
                                 marker=dict(color='crimson', size=8, symbol='x', line=dict(color='white', width=1))))

    # Highlight anomalies if configured and UCL known (always show)
    if int(eff.get('CHART_MARK_ANOMALIES', 1)) and ucl_value:
        anoms = [(h.timestamp, h.response_time_ms) for h in history if h.is_ok and (h.response_time_ms or 0) > ucl_value]
        if anoms:
            a_ts, a_rt = zip(*anoms)
            fig.add_trace(go.Scatter(x=list(a_ts), y=list(a_rt), mode='markers', name='Аномалія',
                                     marker=dict(color='orange', size=9, line=dict(color='white', width=0.7))))
    
    # Plot average response time line (as a trace so it appears in legend)
    if avg_response_time:
        try:
            x0 = timestamps[0]
            x1 = timestamps[-1]
            fig.add_trace(go.Scatter(
                x=[x0, x1], y=[avg_response_time, avg_response_time], mode='lines',
                name=f'Сер. час ({int(avg_response_time)} мс)',
                line=dict(color='darkorange', width=1.5, dash='dash'), hoverinfo='skip'
            ))
        except Exception:
            pass

    # EWMA лінія (конфігурована)
    if int(eff.get('CHART_SHOW_EWMA', 1)):
        try:
            alpha = float(eff.get('CHART_EWMA_ALPHA', 0.3) or 0.3)
            ew = float(response_times[0])
            ew_series = [ew]
            for v in response_times[1:]:
                ew = alpha * v + (1 - alpha) * ew
                ew_series.append(ew)
            fig.add_trace(go.Scatter(x=timestamps, y=ew_series, mode='lines', name=f'EWMA (α={alpha:g})',
                                     line=dict(color='rebeccapurple', width=1.7)))
        except Exception:
            pass

    # UCL лінія (як трейс для легенди)
    if int(eff.get('CHART_SHOW_UCL', 1)) and ucl_value:
        try:
            x0 = timestamps[0]
            x1 = timestamps[-1]
            fig.add_trace(go.Scatter(
                x=[x0, x1], y=[ucl_value, ucl_value], mode='lines',
                name=f'UCL (~{int(ucl_value)} мс)',
                line=dict(color='crimson', width=1.6, dash='dot'), hoverinfo='skip'
            ))
        except Exception:
            pass

    # Shade downtime intervals derived from history (consecutive is_ok=False)
    try:
        down_start = None
        for h in history:
            if not h.is_ok and down_start is None:
                down_start = h.timestamp
            if h.is_ok and down_start is not None:
                fig.add_vrect(x0=down_start, x1=h.timestamp, fillcolor='lightcoral', opacity=0.12, line_width=0)
                down_start = None
        if down_start is not None:
            fig.add_vrect(x0=down_start, x1=timestamps[-1], fillcolor='lightcoral', opacity=0.12, line_width=0)
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
                    q = quantiles(vals, n=100)[pi-1]
                    fig.add_hline(y=q, line_color='gray', line_dash='dash', line_width=0.8,
                                  annotation_text=f'P{pi}≈{int(q)} мс', annotation_position='top left')
    except Exception:
        pass

    # Formatting
    subtitle = get_period_text(period)
    if subtitle_bits:
        subtitle += " · " + ", ".join(subtitle_bits)

    yscale = (str(eff.get('CHART_Y_SCALE') or 'log')).lower()
    y_type = 'log' if (yscale == 'log' or (yscale == 'auto' and (max(response_times) - min(response_times)) >= 100 and max(response_times) > 0)) else 'linear'

    # Legend styling (below chart). Adjust background for better readability.
    is_dark = (template == 'plotly_dark')
    legend_bg = 'rgba(0,0,0,0.35)' if is_dark else 'rgba(255,255,255,0.75)'
    legend_border = '#444' if is_dark else '#dddddd'
    fig.update_layout(
        template=template,
        title=f"Статистика відповіді для '{api_name}'\n за {subtitle}",
        width=width_px,
        height=height_px,
        margin=dict(l=60, r=30, t=70, b=90),
    legend=dict(
            orientation='h', yanchor='top', y=-0.18, xanchor='left', x=0,
            bgcolor=legend_bg, bordercolor=legend_border, borderwidth=1, font=dict(size=11)
    ),
    legend_traceorder='normal'
    )
    fig.update_xaxes(title_text='Час')
    fig.update_yaxes(title_text='Час відповіді (мс)', type=y_type)

    buf = io.BytesIO()
    scale = max(1, int((eff.get('CHART_DPI', 120) or 120) / 96))
    pio.write_image(fig, buf, format='png', scale=scale)
    buf.seek(0)
    return buf

def _safe_pct(x: float) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0

async def generate_daily_overview_chart(items: List[Dict[str, Any]], overrides: Optional[Dict[str, Any]] = None) -> io.BytesIO:
    """Створює оглядовий графік за 24h для всіх моніторів.
    Очікує список словників із ключами: name, avg_ms, uptime, downtime_min, incidents, anomalies.
    Повертає PNG у BytesIO.
    """
    eff = _effective_chart_config(overrides)
    # Тема
    template = 'plotly_dark' if str(eff.get('CHART_STYLE') or '').lower().find('dark') >= 0 else 'plotly_white'
    # Розміри: висота залежить від кількості рядків
    try:
        w, _ = (float(x) for x in (eff.get('CHART_SIZE') or "12x6.5").lower().split('x'))
    except Exception:
        w = 12.0
    width_px = int(w * 75)
    n = max(1, len(items))
    height_px = max(420, int(120 + n * 30))

    if not items:
        fig = go.Figure()
        fig.add_annotation(text='Немає активних моніторів для щоденного звіту', x=0.5, y=0.5,
                           xref='paper', yref='paper', showarrow=False,
                           font=dict(size=16, color='gray'))
        fig.update_layout(width=width_px, height=height_px, template=template)
        buf = io.BytesIO()
        pio.write_image(fig, buf, format='png', scale=max(1, int((eff.get('CHART_DPI', 120) or 120)/96)))
        buf.seek(0)
        return buf

    # Сортуємо за середнім часом відповіді (спадання)
    items_sorted = sorted(items, key=lambda x: (x.get('avg_ms') or 0), reverse=True)
    # Додаємо бейджі UP/DOWN до імен
    names = [f"{'🟢' if bool(it.get('is_up', True)) else '🔴'} {str(it.get('name'))}" for it in items_sorted]
    avg_ms = [int(it.get('avg_ms') or 0) for it in items_sorted]
    uptime = [float(it.get('uptime') or 0.0) for it in items_sorted]
    down_min = [int(it.get('downtime_min') or 0) for it in items_sorted]
    incs = [int(it.get('incidents') or 0) for it in items_sorted]
    anos = [int(it.get('anomalies') or 0) for it in items_sorted]

    from plotly.subplots import make_subplots
    fig = make_subplots(rows=1, cols=3, shared_yaxes=True, horizontal_spacing=0.1,
                        subplot_titles=("Сер. час відповіді (мс)", "Простій (хв)", "Інциденти (шт)"))

    # Бар для середнього часу відповіді
    fig.add_trace(
        go.Bar(x=avg_ms, y=names, orientation='h', name='Avg RT',
               marker=dict(color=uptime, colorscale='RdYlGn', cmin=0, cmax=100, showscale=False),
               text=[f"{u:.1f}% · інц {i} · ан {a}" for u,i,a in zip(uptime, incs, anos)],
               textposition='auto'),
        row=1, col=1
    )

    # Бар для простою (хвилини)
    fig.add_trace(
        go.Bar(x=down_min, y=names, orientation='h', name='Downtime', marker_color='indianred',
               text=[str(v) if v>0 else '' for v in down_min], textposition='auto'),
        row=1, col=2
    )

    # Степ-бар для кількості інцидентів
    fig.add_trace(
        go.Bar(x=incs, y=names, orientation='h', name='Incidents', marker_color='darkorange',
               text=[str(v) if v>0 else '' for v in incs], textposition='auto'),
        row=1, col=3
    )

    fig.update_layout(
        template=template,
        width=width_px,
        height=height_px,
        margin=dict(l=160, r=40, t=80, b=40),
        showlegend=False,
        title="Огляд моніторів · 24h"
    )
    fig.update_xaxes(title_text='мс', row=1, col=1)
    fig.update_xaxes(title_text='хв', row=1, col=2)
    fig.update_xaxes(title_text='шт', row=1, col=3)
    fig.update_yaxes(autorange='reversed')

    buf = io.BytesIO()
    scale = max(1, int((eff.get('CHART_DPI', 120) or 120)/96))
    pio.write_image(fig, buf, format='png', scale=scale)
    buf.seek(0)
    return buf

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