# Telegram PT Debrief Bot

Бот принимает PDF PT-отчёт, делает развёрнутый дебриф через OpenAI Responses API, строит инфографику через matplotlib и ведёт диалог из 3 шагов.

Логика диалога:
- шаг 1/3: initial debrief + 3 кнопки с готовыми сообщениями;
- шаг 2/3: углубление после выбора готовой кнопки или ручного сообщения пользователя;
- шаг 3/3: финальное углубление.

После шага 3/3 бот завершает сессию и предлагает начать заново через `/start`.

## Что умеет

- принимает только `Message.document` с расширением `.pdf`;
- ограничивает размер PDF до 20MB;
- кодирует PDF в base64 `data_url` и передаёт в OpenAI как `input_file`;
- использует строгий `json_schema` (`debrief_text` + `ready_messages[3]`);
- показывает 3 готовых сообщения-кнопки (вместо вопросов), а также принимает свой вопрос/сообщение текстом;
- добавляет инфографику PNG по отчёту (данные извлекаются моделью строго из PDF, график строится в matplotlib);
- работает на `aiogram >= 3.7.0` с синтаксисом `Bot(..., default=DefaultBotProperties(...))`;
- хранит состояние сессии только в FSM.

## Структура файлов

- `bot.py` — основная логика Telegram-бота;
- `prompts.py` — системный промпт и инструкции для модели;
- `bot_texts.py` — все тексты, которые отправляет бот пользователю;
- `requirements.txt` — зависимости;
- `.env.example` — пример переменных окружения.

## Подготовка

1. Скопируйте пример env:

```bash
cp .env.example .env
```

2. Заполните `.env`:

- `BOT_TOKEN` — токен Telegram-бота
- `OPENAI_API_KEY` — ключ OpenAI API

## Установка и запуск (Linux / macOS)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

## Установка и запуск (Windows PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python .\bot.py
```
