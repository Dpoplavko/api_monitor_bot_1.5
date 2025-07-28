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
    BOT_TOKEN=os.getenv("BOT_TOKEN"),
    ADMIN_USER_ID=os.getenv("ADMIN_USER_ID"),
    DATABASE_URL=os.getenv("DATABASE_URL"),
    # Встановлюємо значення за замовчуванням, якщо вони не вказані в .env
    FAILURE_THRESHOLD=int(os.getenv("FAILURE_THRESHOLD", 3)),
    RECOVERY_THRESHOLD=int(os.getenv("RECOVERY_THRESHOLD", 2))
)