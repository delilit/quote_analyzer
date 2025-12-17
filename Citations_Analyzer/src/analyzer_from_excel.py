import pandas as pd
import os
import re
import tempfile
from collections import Counter
from typing import List, Tuple

# Частые служебные слова — исключаются из анализа
STOPWORDS = set("""
a an the and or of for to in on at from into onto about over under as by is are be
with without within between among that this those these their its it's his her was
were been shall will may can could would should else other another also etc some any
if then but so not nor than such both because however although though while during
""".split())

# Русские тоже добавим, если вдруг
STOPWORDS.update("""
и в во не что от до над под при про по из за же ли но а о е я ты мы он она они оно
""".split())

def normalize_text(s: str) -> List[str]:
    """
    Приводит строку к списку слов:
    - нижний регистр
    - удаляет спецсимволы
    - сохраняет дефисные слова отдельно (data-driven → "data-driven", "data", "driven")
    """
    if not isinstance(s, str):
        return []

    s = s.lower().strip()

    # Выделяем слова и дефисные связки:
    #    "data-driven" -> ['data-driven']
    hyphen_words = re.findall(r'\b[\w]+\-[\w]+\b', s)

    # Убираем дефисы для дополнительного деления
    s_clean = re.sub(r'[^a-zA-Zа-яА-ЯёЁ0-9\- ]', ' ', s)
    words = s_clean.split()

    # Добавляем как отдельные слова из дефиса
    expanded = []
    for w in words:
        if '-' in w:
            expanded.append(w)             # целиком
            expanded.extend(w.split('-'))  # части
        else:
            expanded.append(w)

    # Удаляем стоп-слова и цифры
    cleaned = [
        w for w in expanded
        if w not in STOPWORDS and not w.isdigit()
    ]

    return cleaned

def get_ngrams(words: List[str], n: int) -> List[Tuple[str, ...]]:
    """Генератор n-грамм (словосочетаний)."""
    return [tuple(words[i:i+n]) for i in range(len(words)-n+1)]

def analyze_titles_from_excel(excel_path: str,
    top_n_words: int = 15,
    top_n_phrases: int = 15) -> str:
    """
    Загружает excel, извлекает названия статей со всех листов,
    анализирует частоту слов и словосочетаний, сохраняет результат в txt.
    """

    if not os.path.exists(excel_path):
        raise FileNotFoundError(f"Файл не найден: {excel_path}")

    dfs = pd.read_excel(excel_path, sheet_name=None)

    all_words = []
    all_titles = []

    for sheetname, df in dfs.items():

        # Пропускаем пустые листы
        if df.empty:
            continue

        # Пытаемся извлечь колонку с названием
        title_column = None
        for col in df.columns:
            normalized = col.lower().strip()
            if "name" in normalized or "назван" in normalized or "title" in normalized:
                title_column = col
                break

        if not title_column:
            continue

        titles = df[title_column].dropna().astype(str).tolist()
        all_titles.extend(titles)

        for title in titles:
            all_words.extend(normalize_text(title))

    # Считаем частоту одиночных слов
    word_counter = Counter(all_words)
    top_words = word_counter.most_common(top_n_words)

    # Биграммы и триграммы
    bigrams = []
    trigrams = []

    for title in all_titles:
        tokens = normalize_text(title)
        bigrams.extend(get_ngrams(tokens, 2))
        trigrams.extend(get_ngrams(tokens, 3))

    bigram_counter = Counter(bigrams).most_common(top_n_phrases)
    trigram_counter = Counter(trigrams).most_common(top_n_phrases)

    # Формируем текст отчёта
    result_lines = ["Топ ключевых слов:\n"]
    for w, c in top_words:
        result_lines.append(f"{w}: {c}")

    result_lines.append("\n\nТоп биграмм:\n")
    for bg, c in bigram_counter:
        result_lines.append(f"{' '.join(bg)}: {c}")

    result_lines.append("\n\nТоп триграмм:\n")
    for tg, c in trigram_counter:
        result_lines.append(f"{' '.join(tg)}: {c}")

    return "\n".join(result_lines)