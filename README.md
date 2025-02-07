# vk_to_tg_bot
Бот для отправки сообщений из вк в телегу и ответ на них из телеги

Бот написан через DeepSeek автор не несет ответствености за утечки


---

### 1. Создание Telegram-бота
1. Откройте Telegram и найдите бота [BotFather](https://t.me/BotFather).
2. Создайте нового бота с помощью команды /newbot.
3. Запишите токен вашего бота — он понадобится для взаимодействия с Telegram API.

---

### 2. Получение доступа к API ВКонтакте
1. Перейдите в [VK Developer](https://vk.com/dev) и создайте новое приложение типа "Standalone".
2. Получите access_token для доступа к API. Для этого используйте [Implicit Flow](https://vk.com/dev/implicit_flow_user) или [Server-side Authorization](https://vk.com/dev/authcode_flow_user).
3. Убедитесь, что у вашего приложения есть права доступа к сообщениям (`messages`).

---

### 3. Настройка сервера
Для работы бота вам понадобится сервер или хостинг, который будет обрабатывать запросы. Вы можете использовать:
- Python с библиотеками python-telegram-bot и vk_api и python-dotenv.
все они есть в файле requirements.txt