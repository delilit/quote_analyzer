import asyncio
import logging
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters.command import CommandStart
from aiogram.methods import SendMessage
from aiogram.types import BufferedInputFile
from os import path, remove

from src.main import CitationAnalyzer, analyze_citing_articles, analyze_cited_articles
from src.builder import ExcelBuilder
from src.utils import parse_doi_input

api_token = '8365621029:AAHwS8jb4qbpRZkKPDNYZtyAnMPb-YgraeA'

logging.basicConfig(level=logging.INFO)

dp = Dispatcher()
analyzer = CitationAnalyzer
excel_builder = ExcelBuilder()

# Обработчик команды /start
@dp.message(CommandStart())
async def start_command(message: types.Message):
    await message.answer(
        'Здравствуйте! Я - бот для анализа DOI.\n\n'  
        'Для начала работы отправьте один или несколько DOI через пробел.\n'
        'Пример: 10.1126/science.adi1887 10.1038/s41586-023-06924-6'
    )

# Обработчик сообщений, не содержащих команду
@dp.message(F.text)
async def process_doi_input(message: types.Message):
    try:
        input_text = message.text.strip()

        # Игнорируем сообщения-команды
        if input_text.startswith('/'):
            return

        # Если удаётся провести анализ DOI, Excel-файл сохраняется в папку временных файлов.
        analysis_results = analyze_cited_articles(input_text)
        if not analysis_results:
            await message.answer('Произошла ошибка. Проверьте введённые данные и попробуйте ещё раз.')
            return

    except Exception as e:
        logging.error(f'Error processing DOI: {e}')
        await message.answer('Произошла ошибка при обработке запроса. Попробуйте ещё раз.')






async def main():
    bot = Bot(token=api_token)
    await dp.start_polling(bot)