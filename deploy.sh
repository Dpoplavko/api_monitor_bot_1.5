#!/bin/bash
# Скрипт для деплою на AWS EC2

echo "🚀 Запуск деплою API Monitor Bot..."

# Оновлення системи
sudo apt update && sudo apt upgrade -y

# Встановлення Docker, якщо не встановлений
if ! command -v docker &> /dev/null; then
    echo "Встановлення Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sh get-docker.sh
    sudo usermod -aG docker $USER
fi

# Встановлення Docker Compose, якщо не встановлений
if ! command -v docker-compose &> /dev/null; then
    echo "Встановлення Docker Compose..."
    sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
    sudo chmod +x /usr/local/bin/docker-compose
fi

# Клонування або оновлення репозиторію
if [ -d "api-monitor-bot" ]; then
    echo "Оновлення існуючого репозиторію..."
    cd api-monitor-bot
    git pull origin main
else
    echo "Клонування репозиторію..."
    git clone https://github.com/Dpoplavko/api-monitor-bot.git
    cd api-monitor-bot
fi

# Створення .env файлу, якщо він не існує
if [ ! -f .env ]; then
    echo "Створіть файл .env з наступними змінними:"
    echo "BOT_TOKEN=ваш_токен_бота"
    echo "ADMIN_USER_ID=ваш_telegram_id"
    echo "FAILURE_THRESHOLD=3"
    echo "RECOVERY_THRESHOLD=2"
    exit 1
fi

# Зупинка старих контейнерів
docker-compose down

# Збірка та запуск
docker-compose up --build -d

echo "✅ Деплой завершено!"
echo "Перевірте статус: docker-compose logs -f"
