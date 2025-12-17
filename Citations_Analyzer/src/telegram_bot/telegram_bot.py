import asyncio
import concurrent.futures
import logging
from IPython.core.error import TryNext
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters.command import CommandStart
from aiogram.methods import SendMessage
from aiogram.types import BufferedInputFile
from os import path, remove

from src.main import CitationAnalyzer, analyze_cited_articles
from src.builder import ExcelBuilder
from src.metadata_analysis import summarize_from_excel
from src.analyzer_from_excel import analyze_titles_from_excel

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
            return ''

        # Сообщение о том, что процесс начался, которое после завершения анализа будет удалено.
        wip_message = await message.answer('Анализирую статьи...')

        # Т.к. analyze_cited_articles не асинхронная функция, для неё нужно выделить поток вручную.
        # Если удаётся провести анализ DOI, Excel-файл сохраняется в папку временных файлов.
        # В переменную excel_file_path запишется путь к файлу.
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            excel_file_path = await loop.run_in_executor(
                pool, analyze_cited_articles, input_text
            )

        if excel_file_path == '':
            await wip_message.delete()
            await message.answer('Произошла ошибка. Проверьте введённые данные и попробуйте ещё раз.')
            return ''

        await wip_message.delete()
        for msg in await analyze_metadata(excel_file_path):
            await message.answer(msg)

        await message.answer(await analyze_words_frequency(excel_file_path))

        analysed_successfully = await send_excel_file(message, excel_file_path)

        if analysed_successfully == '':
            await message.answer('Произошла ошибка при отправке файла.')
            await delete_temp_file(excel_file_path)
            return ''

        await delete_temp_file(excel_file_path)
        return ''

    except Exception as e:
        logging.error(f'Error processing DOI: {e}')

        await message.answer('Произошла ошибка при обработке запроса. Попробуйте ещё раз.')
        return ''


async def send_excel_file(message: types.Message, file_path):
    try:
        # Проверка, входит ли размер полученного файла в ограничение Telegram.
        file_size_gb = path.getsize(file_path) / (1024 ** 3)
        if file_size_gb > 2:
            await message.answer(
                f'Размер файла отчёта оказался слишком большим ({file_size_gb} Гб).\n'
                'Ограничение Telegram: 2 Гб.\n'
                'Попробуйте уменьшить количество DOI.'
            )
            return ''

        # Рассматриваем файл в бинарном формате.
        file_data = open(file_path, 'rb').read()
        input_file = BufferedInputFile(file_data, filename = path.basename(file_path))
        await message.answer_document(input_file, caption='Анализ данных статей + цитированных'
                                                         ' и цитирующих статей')
        return 1

    except Exception as e:
        logging.error(f"Error sending file: {e}")
        return ''


async def analyze_words_frequency(excel_path):
    return analyze_titles_from_excel(excel_path)


async def analyze_metadata(excel_path):
    max_symbols_count = 4096
    metadata_analysis = summarize_from_excel(excel_path)

    # Список сообщений со статистикой, которые отправит бот.
    _messages = []
    current_message = ''

    # Запоминаем последнюю категорию, чтобы не потерять сообщение, если вся статистика уместится
    # в одно сообщение.
    last_category = list(metadata_analysis.items())[-1][0]

    # Формируем сообщения.
    for category, counter in metadata_analysis.items():
        # Проверяем, войдёт ли целиком блок статистики в одно сообщение, учитывая, что
        # в сообщении могут быть ещё блоки.
        if len(current_message) + len(counter) + len(category) + 2 <= max_symbols_count:
            current_message += category + '\n\n'
            for value, count in counter:
                current_message += f'• {value}: {count}\n'
            current_message += '\n\n'

        else:
            # Если сообщение не пустое, добавляем его в список сообщений, очищаем строку текущего
            # сообщения под следующее.
            if current_message:
                _messages.append(current_message.strip())

                current_message = ''
                for value,count in counter:
                    current_message += f'• {value}: {count}\n'
                    current_message += '\n\n'
                    continue

            # Если сообщение оказалось пустым, вызывается исключение
            else:
                raise Exception('Блок статистики не умещается в 4096 символов!')

        # Если текущее сообщение не пустое, и это последняя итерация — добавляем в список.
        if last_category == category and current_message:
            _messages.append(current_message.strip())

    return _messages


async def delete_temp_file(file_path):
    try:
        if path.exists(file_path):
            remove(file_path)
            logging.info(f'Удалён временный файл: {file_path}')

    except Exception as e:
        logging.warning(f"Could not delete temp file: {e}")


async def main():
    bot = Bot(token=api_token)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())