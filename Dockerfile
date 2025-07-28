# Dockerfile
# Інструкції для збірки Docker-образу нашого застосунку

# 1. Використовуємо офіційний образ Python як базовий
FROM python:3.10-slim

# 2. Встановлюємо системні залежності для matplotlib
# fontconfig потрібен для рендерингу тексту на графіках
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

# 3. Встановлюємо робочу директорію всередині контейнера
WORKDIR /app

# 4. Копіюємо файл з залежностями
COPY requirements.txt .

# 5. Встановлюємо Python залежності
RUN pip install --no-cache-dir -r requirements.txt

# 6. Копіюємо решту коду нашого застосунку
COPY ./src /app/src

# 7. Вказуємо команду, яка буде виконана при запуску контейнера
CMD ["python", "src/bot.py"]