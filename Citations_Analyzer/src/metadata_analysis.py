import os
import pandas as pd
import tempfile
from collections import Counter
from typing import Dict, List

# Каждому столбцу дадим "синонимы",
# чтобы можно было находить их в разных Excel-файлах
COLUMN_ALIASES = {
    "year": ["год", "year", "publication_year"],
    "publisher": ["издатель", "publisher"],
    "journal_full": ["полное наименование журнала", "full journal name", "journal_full"],
    "journal_short": ["сокращ", "abbrev", "short journal name"],
    "authors": ["авторы", "authors"],
    "affiliation": ["аффилиация", "affiliation", "affiliations"]
}


def detect_column(df: pd.DataFrame, aliases: List[str]) -> str | None:
    """
    Находит колонку по списку вариантов названия.
    """
    for col in df.columns:
        cname = col.lower().strip()
        for a in aliases:
            if a.lower() in cname:
                return col
    return None


def extract_list_from_cell(value) -> List[str]:
    """
    Превращает строки вида:
    "Иванов И., Петров П."
    "MIT; Harvard / Oxford"
    в список отдельных элементов.
    """
    if isinstance(value, str):
        for sep in [";", ",", "/"]:
            if sep in value:
                return [v.strip() for v in value.split(sep) if v.strip()]
        return [value.strip()]
    return []


def summarize_from_excel(
    excel_path: str,
    max_display: int = 20,
):
    """
    Загружает Excel файл, анализирует:
    - годы
    - издателей
    - журналы
    - авторов
    - аффилиации
    """

    if not os.path.exists(excel_path):
        raise FileNotFoundError(f"Файл не найден: {excel_path}")

    sheets = pd.read_excel(excel_path, sheet_name=None)

    counter_years = Counter()
    counter_publishers = Counter()
    counter_journals = Counter()
    counter_authors = Counter()
    counter_affiliations = Counter()

    # Словарь, в котором будут храниться каунтеры соответствующих категорий
    results_dict = {}

    for sheet_name, df in sheets.items():
        if df.empty:
            continue

        # ГОД
        col_year = detect_column(df, COLUMN_ALIASES["year"])
        if col_year and col_year in df.columns:
            years = (
                df[col_year]
                .dropna()
                .astype(str)
                .str.extract(r"(\d{4})")[0]
                .dropna()
                .tolist()
            )
            counter_years.update(years)

        results_dict['Года'] = counter_years.most_common(max_display)

        # ИЗДАТЕЛЬ
        col_pub = detect_column(df, COLUMN_ALIASES["publisher"])
        if col_pub and col_pub in df.columns:
            pubs = df[col_pub].dropna().astype(str).tolist()
            counter_publishers.update(pubs)

        results_dict['Издатели'] = counter_publishers.most_common(max_display)

        # ЖУРНАЛ
        col_journal = detect_column(df, COLUMN_ALIASES["journal_full"])
        if col_journal and col_journal in df.columns:
            journs = df[col_journal].dropna().astype(str).tolist()
            counter_journals.update(journs)
        else:
            col_journal = detect_column(df, COLUMN_ALIASES["journal_short"])
            if col_journal and col_journal in df.columns:
                journs = df[col_journal].dropna().astype(str).tolist()
                counter_journals.update(journs)

        results_dict['Журналы'] = counter_journals.most_common(max_display)

        # АВТОРЫ
        col_auth = detect_column(df, COLUMN_ALIASES["authors"])
        if col_auth and col_auth in df.columns:
            for cell in df[col_auth].dropna().tolist():
                counter_authors.update(extract_list_from_cell(cell))

        results_dict['Авторы'] = counter_authors.most_common(max_display)

        # АФФИЛИАЦИИ
        col_aff = detect_column(df, COLUMN_ALIASES["affiliation"])
        if col_aff and col_aff in df.columns:
            for cell in df[col_aff].dropna().tolist():
                counter_affiliations.update(extract_list_from_cell(cell))

        results_dict['Аффилиации'] = counter_affiliations.most_common(max_display)

    return results_dict
