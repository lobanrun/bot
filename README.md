# Telegram PT Debrief Bot

Бот принимает PDF PT-отчёт, делает краткий дебриф через OpenAI Responses API и проводит диалог из 3 шагов:

- шаг 1/3: initial debrief + 3 вопроса-кнопки;
- шаг 2/3: углубление после ответа пользователя;
- шаг 3/3: финальное углубление после ответа пользователя.

После шага 3/3 бот завершает сессию и предлагает начать заново через `/start`.

## Что умеет

- принимает только `Message.document` с расширением `.pdf`;
- ограничивает размер PDF до 20MB;
- кодирует PDF в base64 `data_url` и передаёт в OpenAI как `input_file`;
- использует строгий `json_schema` (`debrief_text` + `next_questions[3]`);
- работает на `aiogram >= 3.7.0` с новым синтаксисом `Bot(..., default=DefaultBotProperties(...))`;
- хранит состояние сессии только в FSM (без глобального состояния для диалога).

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

## Использование

1. Отправьте `/start`.
2. Пришлите PDF документом.
3. Нажмите одну из 3 inline-кнопок с вопросом.
4. Ответьте одним текстовым сообщением.
5. Повторите для следующих шагов, пока не достигнете лимита 3/3.
