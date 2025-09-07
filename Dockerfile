# Dockerfile
# Інструкції для збірки Docker-образу нашого застосунку

# 1. Використовуємо офіційний образ Python як базовий
FROM python:3.10-slim

# 2. Встановлюємо системні залежності для matplotlib та таймзони
# fontconfig/DejaVu забезпечують шрифти, libfreetype/libpng — рендеринг PNG, tzdata — коректні часові зони
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    fontconfig \
    fonts-dejavu \
    libfreetype6 \
    libpng16-16 \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 3. Встановлюємо робочу директорію всередині контейнера
WORKDIR /app

# 4. Копіюємо файл з залежностями
COPY requirements.txt .

# 5. Встановлюємо Python залежності
RUN pip install --no-cache-dir -r requirements.txt

# 6. Копіюємо решту коду нашого застосунку
COPY ./src /app/src
# Copy entrypoint for permission fix (WAL)
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# 6.1. Створюємо каталог для SQLite
RUN mkdir -p /app/data

# 6.2. Створюємо користувача без прав root
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
# Keep root for entrypoint chown, will drop privileges when starting
# We'll still run Python as appuser via entrypoint

# 7. Налаштовуємо таймзону всередині контейнера (може бути перевизначена змінною оточення TZ)
ENV TZ=UTC

# 8. Вказуємо команду, яка буде виконана при запуску контейнера
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 CMD wget -qO- http://127.0.0.1:${METRICS_PORT:-8080}/ || exit 1
ENTRYPOINT ["/app/entrypoint.sh"]