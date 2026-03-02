import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from openai import OpenAI

from bot_texts import (
    CALLBACK_INVALID_TEXT,
    CALLBACK_NOT_FOUND_TEXT,
    CHART_CAPTION,
    CUSTOM_INPUT_FALLBACK_TEXT,
    FILE_TOO_LARGE_TEXT,
    ITERATION_ERROR_TEXT,
    ITERATION_LIMIT_TEXT,
    ITERATION_START_TEXT,
    NOT_PDF_TEXT,
    OPENAI_ERROR_TEXT,
    PDF_ACCEPTED_TEXT,
    PDF_ONLY_TEXT,
    START_TEXT,
    STEP_PROMPT_TEXT,
)
from prompts import (
    INFOGRAPHIC_SYSTEM_PROMPT,
    INITIAL_INSTRUCTION,
    ITERATION_INSTRUCTION_TEMPLATE,
    NEGATIVE_PROMPT,
    SYSTEM_PROMPT,
)

load_dotenv()

MAX_PDF_SIZE_BYTES = 20 * 1024 * 1024
MAX_TELEGRAM_MESSAGE_LENGTH = 4096
TMP_DIR = Path("tmp")


class DebriefStates(StatesGroup):
    waiting_pdf = State()
    waiting_input = State()


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


def build_ready_messages_keyboard(ready_messages: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=item[:120], callback_data=f"pick:{idx}")]
            for idx, item in enumerate(ready_messages)
        ]
    )


def debrief_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "debrief_text": {"type": "string"},
            "ready_messages": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {"type": "string"},
            },
        },
        "required": ["debrief_text", "ready_messages"],
        "additionalProperties": False,
    }


def infographic_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "items": {
                "type": "array",
                "minItems": 3,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "value": {"type": "number", "minimum": 0, "maximum": 100},
                    },
                    "required": ["label", "value"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["title", "items"],
        "additionalProperties": False,
    }


def call_openai_debrief(client: OpenAI, pdf_name: str, pdf_data_url: str, user_instruction: str) -> dict[str, Any]:
    response = client.responses.create(
        model="gpt-4o-mini",
        temperature=0.3,
        input=[
            {
                "role": "system",
                "content": [
                    {"type": "input_text", "text": SYSTEM_PROMPT},
                    {"type": "input_text", "text": NEGATIVE_PROMPT},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "input_file", "filename": pdf_name, "file_data": pdf_data_url},
                    {"type": "input_text", "text": user_instruction},
                ],
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "pt_debrief",
                "schema": debrief_schema(),
                "strict": True,
            }
        },
    )
    parsed = json.loads(response.output_text)
    ready_messages = parsed.get("ready_messages")
    if (
        not isinstance(parsed, dict)
        or not isinstance(parsed.get("debrief_text"), str)
        or not isinstance(ready_messages, list)
        or len(ready_messages) != 3
        or any(not isinstance(item, str) for item in ready_messages)
    ):
        raise ValueError("Некорректный формат ответа debrief.")
    return parsed


def call_openai_infographic(client: OpenAI, pdf_name: str, pdf_data_url: str, context_text: str) -> dict[str, Any]:
    response = client.responses.create(
        model="gpt-4o-mini",
        temperature=0.2,
        input=[
            {
                "role": "system",
                "content": [
                    {"type": "input_text", "text": INFOGRAPHIC_SYSTEM_PROMPT},
                    {"type": "input_text", "text": NEGATIVE_PROMPT},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "input_file", "filename": pdf_name, "file_data": pdf_data_url},
                    {"type": "input_text", "text": f"Контекст дебрифа:\n{context_text}"},
                ],
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "pt_infographic",
                "schema": infographic_schema(),
                "strict": True,
            }
        },
    )
    parsed = json.loads(response.output_text)
    if not isinstance(parsed, dict):
        raise ValueError("Некорректный формат инфографики.")
    return parsed


def render_infographic(chat_id: int, step: int, payload: dict[str, Any]) -> Path:
    title = str(payload.get("title", "Ключевые акценты отчёта"))
    items = payload.get("items", [])
    labels = [str(item["label"])[:40] for item in items]
    values = [float(item["value"]) for item in items]

    plt.style.use("seaborn-v0_8")
    fig, ax = plt.subplots(figsize=(10, 6))
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#17becf"]
    bars = ax.barh(labels, values, color=colors[: len(labels)])
    ax.set_xlim(0, 100)
    ax.set_xlabel("Относительная важность (0-100)")
    ax.set_title(title)

    for bar, value in zip(bars, values):
        ax.text(value + 1, bar.get_y() + bar.get_height() / 2, f"{value:.0f}", va="center")

    fig.tight_layout()
    out_path = TMP_DIR / f"chart_{chat_id}_{step}.png"
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return out_path


async def send_step_output(
    message: Message,
    client: OpenAI,
    pdf_name: str,
    pdf_data_url: str,
    debrief_text: str,
    ready_messages: list[str],
    step: int,
) -> None:
    for part in split_text(debrief_text):
        await message.answer(part)

    try:
        infographic_data = await asyncio.to_thread(
            call_openai_infographic, client, pdf_name, pdf_data_url, debrief_text
        )
        chart_path = await asyncio.to_thread(render_infographic, message.chat.id, step, infographic_data)
        await message.answer_photo(photo=FSInputFile(chart_path), caption=CHART_CAPTION)
    except Exception:
        logging.exception("Не удалось построить инфографику")

    await message.answer(
        STEP_PROMPT_TEXT.format(step=step + 1),
        reply_markup=build_ready_messages_keyboard(ready_messages),
    )


def build_history_text(history: list[dict[str, str]]) -> str:
    if not history:
        return "(история пока пустая)"
    return "\n".join([f"{i+1}) {item['user_input']}" for i, item in enumerate(history)])


async def run_model_step(message: Message, state: FSMContext, client: OpenAI, user_input: str | None) -> None:
    data = await state.get_data()
    pdf_name = data["pdf_name"]
    pdf_data_url = data["pdf_data_url"]
    step = int(data.get("step", 0))
    history = list(data.get("history", []))

    if user_input is None:
        instruction = INITIAL_INSTRUCTION
    else:
        history.append({"user_input": user_input})
        instruction = ITERATION_INSTRUCTION_TEMPLATE.format(
            last_debrief=data["last_debrief_text"],
            history=build_history_text(history),
            user_input=user_input,
        )

    result = await asyncio.to_thread(call_openai_debrief, client, pdf_name, pdf_data_url, instruction)
    debrief_text = result["debrief_text"]
    ready_messages = result["ready_messages"]

    new_step = step if user_input is None else step + 1

    await state.update_data(
        step=new_step,
        last_debrief_text=debrief_text,
        ready_messages=ready_messages,
        history=history,
    )
    await state.set_state(DebriefStates.waiting_input)

    await send_step_output(
        message=message,
        client=client,
        pdf_name=pdf_name,
        pdf_data_url=pdf_data_url,
        debrief_text=debrief_text,
        ready_messages=ready_messages,
        step=new_step,
    )


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    openai_api_key = os.getenv("OPENAI_API_KEY")

    if not token:
        raise RuntimeError("BOT_TOKEN не найден в переменных окружения.")
    if not openai_api_key:
        raise RuntimeError("OPENAI_API_KEY не найден в переменных окружения.")

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO)

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher(storage=MemoryStorage())
    openai_client = OpenAI(api_key=openai_api_key)

    @dp.message(Command("start"))
    async def start_handler(message: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(DebriefStates.waiting_pdf)
        await message.answer(START_TEXT)

    @dp.message(DebriefStates.waiting_pdf, F.document)
    async def pdf_handler(message: Message, state: FSMContext) -> None:
        document = message.document
        if document is None:
            await message.answer(PDF_ONLY_TEXT)
            return

        file_name = document.file_name or "report.pdf"
        if not file_name.lower().endswith(".pdf"):
            await message.answer(NOT_PDF_TEXT)
            return

        if document.file_size and document.file_size > MAX_PDF_SIZE_BYTES:
            await message.answer(FILE_TOO_LARGE_TEXT)
            return

        user_id = message.from_user.id if message.from_user else 0
        safe_name = Path(file_name).name
        file_path = TMP_DIR / f"{user_id}_{document.file_unique_id}_{safe_name}"
        await bot.download(document, destination=file_path)

        pdf_bytes = file_path.read_bytes()
        pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
        pdf_data_url = f"data:application/pdf;base64,{pdf_base64}"

        await state.update_data(
            pdf_name=safe_name,
            pdf_data_url=pdf_data_url,
            step=0,
            last_debrief_text="",
            ready_messages=[],
            history=[],
        )

        await message.answer(PDF_ACCEPTED_TEXT)
        try:
            await run_model_step(message, state, openai_client, user_input=None)
        except Exception:
            logging.exception("Ошибка initial debrief")
            await message.answer(OPENAI_ERROR_TEXT)

    @dp.message(DebriefStates.waiting_pdf)
    async def waiting_pdf_fallback(message: Message) -> None:
        await message.answer(PDF_ONLY_TEXT)

    @dp.callback_query(DebriefStates.waiting_input, F.data.startswith("pick:"))
    async def pick_ready_message(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        ready_messages = data.get("ready_messages", [])

        try:
            idx = int(callback.data.split(":", maxsplit=1)[1])
        except ValueError:
            await callback.answer(CALLBACK_INVALID_TEXT, show_alert=True)
            return

        if idx < 0 or idx >= len(ready_messages):
            await callback.answer(CALLBACK_NOT_FOUND_TEXT, show_alert=True)
            return

        step = int(data.get("step", 0))
        if step >= 2:
            await callback.message.answer(ITERATION_LIMIT_TEXT)
            await state.clear()
            await callback.answer()
            return

        user_input = str(ready_messages[idx])
        await callback.answer()
        await callback.message.answer(f"Выбрано сообщение:\n\n{user_input}\n\n{ITERATION_START_TEXT}")
        try:
            await run_model_step(callback.message, state, openai_client, user_input=user_input)
        except Exception:
            logging.exception("Ошибка iteration from callback")
            await callback.message.answer(ITERATION_ERROR_TEXT)

    @dp.message(DebriefStates.waiting_input, F.text)
    async def custom_user_input(message: Message, state: FSMContext) -> None:
        user_input = (message.text or "").strip()
        if not user_input:
            await message.answer(CUSTOM_INPUT_FALLBACK_TEXT)
            return

        data = await state.get_data()
        step = int(data.get("step", 0))
        if step >= 2:
            await message.answer(ITERATION_LIMIT_TEXT)
            await state.clear()
            return

        await message.answer(ITERATION_START_TEXT)
        try:
            await run_model_step(message, state, openai_client, user_input=user_input)
        except Exception:
            logging.exception("Ошибка iteration from custom text")
            await message.answer(ITERATION_ERROR_TEXT)

    @dp.message(DebriefStates.waiting_input)
    async def waiting_input_fallback(message: Message) -> None:
        await message.answer(CUSTOM_INPUT_FALLBACK_TEXT)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
