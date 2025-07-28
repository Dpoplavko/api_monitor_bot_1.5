# src/utils.py
# Допоміжні функції (парсер команд, форматування повідомлень)

import datetime
import json
import io
from typing import Dict, Optional, List, Any

# Використовуємо Agg-бекенд для генерації графіків на сервері без GUI
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import ScalarFormatter, NullFormatter

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

async def generate_statistics_chart(history: List["CheckHistory"], api_name: str, period: str, avg_response_time: float) -> io.BytesIO:
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
    response_times = [h.response_time_ms if h.response_time_ms > 0 else 1 for h in history] # Замінюємо 0 на 1 для лог. шкали
    
    ok_checks = [(h.timestamp, h.response_time_ms) for h in history if h.is_ok and h.response_time_ms > 0]
    fail_checks = [(h.timestamp, h.response_time_ms) for h in history if not h.is_ok and h.response_time_ms > 0]

    plt.style.use('seaborn-v0_8-darkgrid')
    fig, ax = plt.subplots(figsize=(12, 6.5))

    # Plot main response time line
    ax.plot(timestamps, response_times, label='Час відповіді (мс)', color='dodgerblue', zorder=2, linewidth=1.5, alpha=0.7)

    # Plot success and failure points
    if ok_checks:
        ok_ts, ok_rt = zip(*ok_checks)
        ax.scatter(ok_ts, ok_rt, color='limegreen', label='Успіх', s=25, zorder=3, alpha=0.8)
    if fail_checks:
        fail_ts, fail_rt = zip(*fail_checks)
        ax.scatter(fail_ts, fail_rt, color='red', label='Збій', s=50, zorder=4, marker='X')
    
    # Plot average response time line
    if avg_response_time:
        ax.axhline(y=avg_response_time, color='darkorange', linestyle='--', linewidth=1.5, label=f'Середній час ({int(avg_response_time)} мс)')

    # Formatting
    ax.set_title(f"Статистика відповіді для '{api_name}'\n за {get_period_text(period)}", fontsize=16, pad=20)
    ax.set_xlabel("Час", fontsize=12)
    ax.set_ylabel("Час відповіді (мс)", fontsize=12)
    ax.legend(loc='upper left')
    ax.set_yscale('log') # Log scale is better for visualizing response time spikes
    
    # --- ПОКРАЩЕННЯ ---
    # Використовуємо звичайні числа замість степенів для осі Y
    ax.yaxis.set_major_formatter(ScalarFormatter())
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.tick_params(axis='y', which='minor', bottom=False)
    # Додаємо більше ліній сітки для кращої читабельності
    ax.grid(True, which='major', linestyle='--', linewidth=0.5)
    ax.grid(True, which='minor', linestyle=':', linewidth=0.3)
    # --- КІНЕЦЬ ПОКРАЩЕНЬ ---

    # Format x-axis dates
    date_format = mdates.DateFormatter('%H:%M\n%d-%m')
    ax.xaxis.set_major_formatter(date_format)
    fig.autofmt_xdate(rotation=0, ha='center')

    fig.tight_layout(pad=2.0)
    
    # Save to buffer
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100)
    buf.seek(0)
    plt.close(fig)
    
    return buf

def format_statistics_report(api_name: str, stats: dict) -> str:
    """Форматує словник зі статистикою в повідомлення для Telegram."""
    period_text = get_period_text(stats['period'])

    report = (
        f"📊 <b>Статистика для '{api_name}' за {period_text}</b>\n\n"
        f"  - <b>Аптайм:</b> {stats['uptime_percent']:.2f}%\n"
        f"  - <b>Середній час відповіді:</b> {int(stats['avg_response_time_ms'] or 0)} мс\n"
        f"  - <b>Кількість падінь:</b> {stats['incident_count']}\n"
        f"  - <b>Загальний час простою:</b> {format_timedelta(stats['total_downtime'])}\n"
        f"  - <b>Середня тривалість падіння:</b> {format_timedelta(stats['avg_downtime'])}"
    )
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