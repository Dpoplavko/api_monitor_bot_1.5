# src/config.py
# Файл для завантаження конфігурації зі змінних середовища

import os
from dataclasses import dataclass
from dotenv import load_dotenv

# Завантажуємо змінні з файлу .env
load_dotenv()

@dataclass
class Settings:
    """
    Клас для зберігання налаштувань, завантажених зі змінних середовища.
    """
    BOT_TOKEN: str
    ADMIN_USER_ID: int
    DATABASE_URL: str | None 
    FAILURE_THRESHOLD: int
    RECOVERY_THRESHOLD: int
    # Optional/advanced
    TZ: str | None = None
    REPORT_HOUR: int = 9
    REPORT_MINUTE: int = 0
    REQUEST_RETRIES: int = 1
    REQUEST_BACKOFF: float = 0.5
    # ML / anomaly detection
    ML_ENABLED: int = 1
    ML_WINDOW: int = 200
    ML_COMPUTE_INTERVAL_MINUTES: int = 10
    ANOMALY_COOLDOWN_MINUTES: int = 30
    # ML detection tuning
    ANOMALY_M: int = 3
    ANOMALY_N: int = 5
    ANOMALY_SENSITIVITY: float = 1.5  # multiplier for threshold (higher => less sensitive)
    ANOMALY_PCT_FACTOR: float = 1.0   # factor for percentile threshold
    ANOMALY_ERROR_RATE_THRESHOLD: float = 0.1  # 10% failures considered elevated
    # Quiet hours & reminders
    QUIET_HOURS_ENABLED: int = 0
    QUIET_START_HOUR: int = 22
    QUIET_END_HOUR: int = 8
    DOWNTIME_REMINDER_MINUTES: int = 60
    # Data retention
    RETENTION_DAYS: int = 90
    # Metrics/health
    METRICS_PORT: int = 8080
    # Charts / visualization
    CHART_STYLE: str | None = "plotly_dark"  # plotly_white / plotly_dark (default: dark)
    CHART_Y_SCALE: str = "log"  # log|linear|auto
    CHART_SHOW_UCL: int = 1
    CHART_SHOW_EWMA: int = 1
    CHART_EWMA_ALPHA: float = 0.3
    CHART_SHOW_PERCENTILES: str = "50,90,95"
    CHART_MARK_FAILURES: int = 1
    # Downsampling & point rendering
    CHART_POINT_EVERY: int = 5  # показувати кожну N-ту точку успішних пінгів
    CHART_MARK_ANOMALIES: int = 1  # завжди показувати точки-аномалії поверх даунсемплінгу
    CHART_SHOW_RAW_LINE: int = 1  # показувати «сиру» лінію з усіх значень
    # Aggregation / downsampling strategy: none | per_minute | lttb
    CHART_AGGREGATION: str = "per_minute"
    CHART_AGG_PERCENTILE: int = 95  # для перехресної лінії в агрегації (P95)
    CHART_LTTB_POINTS: int = 240  # цільова кількість точок для LTTB
    CHART_SIZE: str = "12x6.5"
    CHART_DPI: int = 120

    def __post_init__(self):
        # Перевірка, що обов'язкові змінні існують
        if not self.BOT_TOKEN:
            raise ValueError("Змінна середовища BOT_TOKEN не встановлена.")
        if not self.ADMIN_USER_ID:
            raise ValueError("Змінна середовища ADMIN_USER_ID не встановлена.")
        
        # Конвертація числових значень
        try:
            self.ADMIN_USER_ID = int(self.ADMIN_USER_ID)
            self.FAILURE_THRESHOLD = int(self.FAILURE_THRESHOLD)
            self.RECOVERY_THRESHOLD = int(self.RECOVERY_THRESHOLD)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Помилка конвертації змінних середовища в числа: {e}")

# Створюємо екземпляр налаштувань
settings = Settings(
    BOT_TOKEN=os.getenv("BOT_TOKEN", ""),
    ADMIN_USER_ID=int(os.getenv("ADMIN_USER_ID", "0")),
    DATABASE_URL=os.getenv("DATABASE_URL"),
    # Встановлюємо значення за замовчуванням, якщо вони не вказані в .env
    FAILURE_THRESHOLD=int(os.getenv("FAILURE_THRESHOLD", 3)),
    RECOVERY_THRESHOLD=int(os.getenv("RECOVERY_THRESHOLD", 2)),
    TZ=os.getenv("TZ", "UTC"),
    REPORT_HOUR=int(os.getenv("REPORT_HOUR", "9")),
    REPORT_MINUTE=int(os.getenv("REPORT_MINUTE", "0")),
    REQUEST_RETRIES=int(os.getenv("REQUEST_RETRIES", "1")),
    REQUEST_BACKOFF=float(os.getenv("REQUEST_BACKOFF", "0.5")),
    ML_ENABLED=(
        1 if str(os.getenv("ML_ENABLED", "1")).strip().lower() in {"1","true","yes","y","on","t"} else 0
    ),
    ML_WINDOW=int(os.getenv("ML_WINDOW", "200")),
    ML_COMPUTE_INTERVAL_MINUTES=int(os.getenv("ML_COMPUTE_INTERVAL_MINUTES", "10")),
    ANOMALY_COOLDOWN_MINUTES=int(os.getenv("ANOMALY_COOLDOWN_MINUTES", "30")),
    ANOMALY_M=int(os.getenv("ANOMALY_M", "3")),
    ANOMALY_N=int(os.getenv("ANOMALY_N", "5")),
    ANOMALY_SENSITIVITY=float(os.getenv("ANOMALY_SENSITIVITY", "1.5")),
    ANOMALY_PCT_FACTOR=float(os.getenv("ANOMALY_PCT_FACTOR", "1.0")),
    ANOMALY_ERROR_RATE_THRESHOLD=float(os.getenv("ANOMALY_ERROR_RATE_THRESHOLD", "0.1")),
    QUIET_HOURS_ENABLED=(
        1 if str(os.getenv("QUIET_HOURS_ENABLED", "0")).strip().lower() in {"1","true","yes","y","on","t"} else 0
    ),
    QUIET_START_HOUR=int(os.getenv("QUIET_START_HOUR", "22")),
    QUIET_END_HOUR=int(os.getenv("QUIET_END_HOUR", "8")),
    DOWNTIME_REMINDER_MINUTES=int(os.getenv("DOWNTIME_REMINDER_MINUTES", "60")),
    RETENTION_DAYS=int(os.getenv("RETENTION_DAYS", "90")),
    METRICS_PORT=int(os.getenv("METRICS_PORT", "8080")),
    CHART_STYLE=os.getenv("CHART_STYLE", "plotly_dark"),
    CHART_Y_SCALE=os.getenv("CHART_Y_SCALE", "log"),
    CHART_SHOW_UCL=(1 if str(os.getenv("CHART_SHOW_UCL", "1")).strip().lower() in {"1","true","yes","y","on","t"} else 0),
    CHART_SHOW_EWMA=(1 if str(os.getenv("CHART_SHOW_EWMA", "1")).strip().lower() in {"1","true","yes","y","on","t"} else 0),
    CHART_EWMA_ALPHA=float(os.getenv("CHART_EWMA_ALPHA", "0.3")),
    CHART_SHOW_PERCENTILES=os.getenv("CHART_SHOW_PERCENTILES", "50,90,95"),
    CHART_MARK_FAILURES=(1 if str(os.getenv("CHART_MARK_FAILURES", "1")).strip().lower() in {"1","true","yes","y","on","t"} else 0),
    CHART_POINT_EVERY=int(os.getenv("CHART_POINT_EVERY", "5")),
    CHART_MARK_ANOMALIES=(1 if str(os.getenv("CHART_MARK_ANOMALIES", "1")).strip().lower() in {"1","true","yes","y","on","t"} else 0),
    CHART_SHOW_RAW_LINE=(1 if str(os.getenv("CHART_SHOW_RAW_LINE", "1")).strip().lower() in {"1","true","yes","y","on","t"} else 0),
    CHART_AGGREGATION=os.getenv("CHART_AGGREGATION", "per_minute"),
    CHART_AGG_PERCENTILE=int(os.getenv("CHART_AGG_PERCENTILE", "95")),
    CHART_LTTB_POINTS=int(os.getenv("CHART_LTTB_POINTS", "240")),
    CHART_SIZE=os.getenv("CHART_SIZE", "12x6.5"),
    CHART_DPI=int(os.getenv("CHART_DPI", "120")),
)