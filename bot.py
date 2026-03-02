import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

SYSTEM_PROMPT = (
    "Ты аналитик отчётов PT. Работай строго по документу. "
    "Не добавляй факты, которых нет в отчёте. Верни только релевантный дебриф "
    "и 3 вопроса для продолжения. Без воды, без общих психологических рассуждений."
)
MAX_PDF_SIZE_BYTES = 20 * 1024 * 1024
MAX_TELEGRAM_MESSAGE_LENGTH = 4096
TMP_DIR = Path("tmp")


class DebriefStates(StatesGroup):
    waiting_pdf = State()
    waiting_question_choice = State()
    waiting_answer = State()


def split_text(text: str, chunk_size: int = MAX_TELEGRAM_MESSAGE_LENGTH) -> list[str]:
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            split_at = text.rfind("\n", start, end)
            if split_at <= start:
                split_at = text.rfind(" ", start, end)
            if split_at > start:
                end = split_at
        chunks.append(text[start:end].strip())
        start = end

    return [chunk for chunk in chunks if chunk]


def build_questions_keyboard(questions: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=question, callback_data=f"pick:{idx}")]
            for idx, question in enumerate(questions)
        ]
    )


def build_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "debrief_text": {"type": "string"},
            "next_questions": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {"type": "string"},
            },
        },
        "required": ["debrief_text", "next_questions"],
        "additionalProperties": False,
    }


def call_openai_responses(
    *,
    client: OpenAI,
    pdf_name: str,
    pdf_data_url: str,
    user_instruction: str,
) -> dict[str, Any]:
    response = client.responses.create(
        model="gpt-4o-mini",
        temperature=0.3,
        input=[
            {
                "role": "system",
                "content": [{"type": "input_text", "text": SYSTEM_PROMPT}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_file",
                        "filename": pdf_name,
                        "file_data": pdf_data_url,
                    },
                    {"type": "input_text", "text": user_instruction},
                ],
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "pt_debrief",
                "schema": build_json_schema(),
                "strict": True,
            }
        },
    )

    parsed = json.loads(response.output_text)
    if not isinstance(parsed, dict):
        raise ValueError("Некорректный формат ответа модели: ожидался JSON-объект.")

    debrief_text = parsed.get("debrief_text")
    next_questions = parsed.get("next_questions")

    if not isinstance(debrief_text, str):
        raise ValueError("Некорректный формат debrief_text.")
    if not isinstance(next_questions, list) or len(next_questions) != 3 or any(
        not isinstance(item, str) for item in next_questions
    ):
        raise ValueError("Некорректный формат next_questions.")

    return parsed


async def send_debrief_and_questions(
    message: Message,
    debrief_text: str,
    questions: list[str],
    step: int,
) -> None:
    for part in split_text(debrief_text):
        await message.answer(part)

    await message.answer(
        f"Шаг {step + 1}/3. Выберите один вопрос для углубления:",
        reply_markup=build_questions_keyboard(questions),
    )


async def run_initial_debrief(
    message: Message,
    state: FSMContext,
    client: OpenAI,
    pdf_name: str,
    pdf_data_url: str,
) -> None:
    instruction = (
        "Сделай краткий дебриф по PT-отчёту и предложи ровно 3 точных вопроса "
        "для дальнейшего обсуждения."
    )

    result = await asyncio.to_thread(
        call_openai_responses,
        client=client,
        pdf_name=pdf_name,
        pdf_data_url=pdf_data_url,
        user_instruction=instruction,
    )

    debrief_text = result["debrief_text"]
    questions = result["next_questions"]

    await send_debrief_and_questions(message, debrief_text, questions, step=0)

    await state.update_data(
        pdf_name=pdf_name,
        pdf_data_url=pdf_data_url,
        step=0,
        current_questions=questions,
        last_debrief_text=debrief_text,
        history=[],
    )
    await state.set_state(DebriefStates.waiting_question_choice)


def build_iteration_instruction(last_debrief_text: str, history: list[dict[str, str]]) -> str:
    history_block = "\n".join(
        f"{idx + 1}) Вопрос: {item['q']}\nОтвет: {item['a']}"
        for idx, item in enumerate(history)
    )
    return (
        "Предыдущий дебриф:\n"
        f"{last_debrief_text}\n\n"
        "История Q/A:\n"
        f"{history_block}\n\n"
        "Уточни/углуби дебриф, не добавляй фактов вне PDF, верни debrief_text + 3 новых вопроса."
    )


async def run_iteration(message: Message, state: FSMContext, client: OpenAI) -> None:
    data = await state.get_data()
    step = int(data["step"])
    history = list(data.get("history", []))

    instruction = build_iteration_instruction(data["last_debrief_text"], history)

    result = await asyncio.to_thread(
        call_openai_responses,
        client=client,
        pdf_name=data["pdf_name"],
        pdf_data_url=data["pdf_data_url"],
        user_instruction=instruction,
    )

    new_step = step + 1
    debrief_text = result["debrief_text"]
    questions = result["next_questions"]

    await state.update_data(
        step=new_step,
        current_questions=questions,
        last_debrief_text=debrief_text,
        history=history,
        chosen_question=None,
    )
    await state.set_state(DebriefStates.waiting_question_choice)

    await send_debrief_and_questions(message, debrief_text, questions, step=new_step)


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    openai_api_key = os.getenv("OPENAI_API_KEY")

    if not token:
        raise RuntimeError("BOT_TOKEN не найден в переменных окружения.")
    if not openai_api_key:
        raise RuntimeError("OPENAI_API_KEY не найден в переменных окружения.")

    TMP_DIR.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(level=logging.INFO)

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    openai_client = OpenAI(api_key=openai_api_key)

    @dp.message(Command("start"))
    async def start_handler(message: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(DebriefStates.waiting_pdf)
        await message.answer(
            "Пришлите PDF документом: я сделаю краткий дебриф и дам 3 вопроса кнопками, "
            "после чего проведу ещё 2 итерации углубления."
        )

    @dp.message(DebriefStates.waiting_pdf, F.document)
    async def pdf_handler(message: Message, state: FSMContext) -> None:
        document = message.document
        if document is None:
            await message.answer("Пожалуйста, отправьте PDF документом.")
            return

        file_name = document.file_name or "report.pdf"
        if not file_name.lower().endswith(".pdf"):
            await message.answer("Похоже, это не PDF. Пожалуйста, отправьте файл с расширением .pdf")
            return

        if document.file_size and document.file_size > MAX_PDF_SIZE_BYTES:
            await message.answer("Файл слишком большой. Отправьте PDF размером до 20MB.")
            return

        safe_name = Path(file_name).name
        file_path = TMP_DIR / f"{message.from_user.id}_{document.file_unique_id}_{safe_name}"

        await bot.download(document, destination=file_path)

        pdf_bytes = file_path.read_bytes()
        pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
        pdf_data_url = f"data:application/pdf;base64,{pdf_base64}"

        await message.answer("PDF получен, готовлю дебриф...")

        try:
            await run_initial_debrief(
                message=message,
                state=state,
                client=openai_client,
                pdf_name=safe_name,
                pdf_data_url=pdf_data_url,
            )
        except Exception:
            logging.exception("Ошибка при обработке initial debrief")
            await message.answer(
                "Не удалось обработать PDF через OpenAI. Попробуйте отправить файл ещё раз чуть позже."
            )

    @dp.message(DebriefStates.waiting_pdf)
    async def waiting_pdf_fallback(message: Message) -> None:
        await message.answer("Пожалуйста, отправьте PDF документом (не фото и не текст).")

    @dp.callback_query(DebriefStates.waiting_question_choice, F.data.startswith("pick:"))
    async def question_pick_handler(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        questions = data.get("current_questions", [])
        raw_index = callback.data.split(":", maxsplit=1)[1]

        try:
            idx = int(raw_index)
        except ValueError:
            await callback.answer("Некорректный выбор", show_alert=True)
            return

        if idx < 0 or idx >= len(questions):
            await callback.answer("Вопрос не найден", show_alert=True)
            return

        chosen_question = questions[idx]
        await state.update_data(chosen_question=chosen_question)
        await state.set_state(DebriefStates.waiting_answer)

        await callback.message.answer(
            f"Вы выбрали вопрос:\n\n{chosen_question}\n\nНапишите ответ одним сообщением."
        )
        await callback.answer()

    @dp.message(DebriefStates.waiting_question_choice)
    async def waiting_choice_fallback(message: Message) -> None:
        await message.answer("Сначала выберите один из 3 вопросов кнопкой ниже.")

    @dp.message(DebriefStates.waiting_answer, F.text)
    async def answer_handler(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        chosen_question = data.get("chosen_question")
        if not chosen_question:
            await message.answer("Сначала выберите вопрос кнопкой, затем отправьте ответ.")
            await state.set_state(DebriefStates.waiting_question_choice)
            return

        history = list(data.get("history", []))
        history.append({"q": chosen_question, "a": message.text})
        await state.update_data(history=history)

        step = int(data.get("step", 0))
        if step == 2:
            await message.answer(
                "Лимит итераций достигнут (3/3). Для нового PDF начните заново через /start"
            )
            await state.clear()
            return

        await message.answer("Принято. Уточняю дебриф...")
        try:
            await run_iteration(message=message, state=state, client=openai_client)
        except Exception:
            logging.exception("Ошибка при обработке iteration")
            await message.answer(
                "Не удалось выполнить итерацию через OpenAI. Попробуйте ответить ещё раз или начните /start"
            )

    @dp.message(DebriefStates.waiting_answer)
    async def waiting_answer_fallback(message: Message) -> None:
        await message.answer("Пожалуйста, отправьте ответ текстом одним сообщением.")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
