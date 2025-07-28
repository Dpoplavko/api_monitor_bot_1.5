# API Monitor Bot

Telegram-бот для моніторингу API із сповіщеннями про падіння та відновлення сервісів.

## 🚀 Функції

- Моніторинг HTTP/HTTPS API з настройками:
  - Різні HTTP методи (GET, POST, PUT, DELETE, PATCH)
  - Перевірка статус-кодів
  - Перевірка JSON-ключів у відповіді
  - Настройка заголовків та тіла запиту
- Система порогів для детекції падінь та відновлень
- Статистика та графіки продуктивності
- Щоденні звіти
- Управління через Telegram команди

## 📋 Вимоги

- Docker та Docker Compose
- Telegram Bot Token
- AWS EC2 instance (для продакшн деплою)

## 🛠 Встановлення на AWS EC2

1. Клонуйте репозиторій:
```bash
git clone https://github.com/Dpoplavko/api-monitor-bot.git
cd api-monitor-bot