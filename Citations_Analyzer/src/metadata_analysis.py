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
    save_to_txt: bool = True
) -> str:
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

    for sheet_name, df in sheets.items():

        if df.empty:
            continue

        # ==== ГОД ====
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

        # ==== ИЗДАТЕЛЬ ====
        col_pub = detect_column(df, COLUMN_ALIASES["publisher"])
        if col_pub and col_pub in df.columns:
            pubs = df[col_pub].dropna().astype(str).tolist()
            counter_publishers.update(pubs)

        # ==== ЖУРНАЛ ====
        col_journal = detect_column(df, COLUMN_ALIASES["journal_full"])
        if col_journal and col_journal in df.columns:
            journs = df[col_journal].dropna().astype(str).tolist()
            counter_journals.update(journs)
        else:
            col_journal = detect_column(df, COLUMN_ALIASES["journal_short"])
            if col_journal and col_journal in df.columns:
                journs = df[col_journal].dropna().astype(str).tolist()
                counter_journals.update(journs)

        # ==== АВТОРЫ ====
        col_auth = detect_column(df, COLUMN_ALIASES["authors"])
        if col_auth and col_auth in df.columns:
            for cell in df[col_auth].dropna().tolist():
                counter_authors.update(extract_list_from_cell(cell))

        # ==== АФФИЛИАЦИИ ====
        col_aff = detect_column(df, COLUMN_ALIASES["affiliation"])
        if col_aff and col_aff in df.columns:
            for cell in df[col_aff].dropna().tolist():
                counter_affiliations.update(extract_list_from_cell(cell))

    # Формируем секции отчёта
    def top(counter: Counter) -> List[str]:
        return [f"{item}: {count}" for item, count in counter.most_common(max_display)]

    result = []
    result.append(f"АНАЛИЗ ФАЙЛА: {excel_path}")
    result.append("=" * 80 + "\n")

    result.append("РАСПРЕДЕЛЕНИЕ ПО ГОДАМ:")
    result.extend(top(counter_years))
    result.append("\n")

    result.append("РАСПРЕДЕЛЕНИЕ ПО ИЗДАТЕЛЬСТВАМ:")
    result.extend(top(counter_publishers))
    result.append("\n")

    result.append("РАСПРЕДЕЛЕНИЕ ПО ЖУРНАЛАМ:")
    result.extend(top(counter_journals))
    result.append("\n")

    result.append("САМЫЕ ЧАСТЫЕ АВТОРЫ:")
    result.extend(top(counter_authors))
    result.append("\n")

    result.append("САМЫЕ ЧАСТЫЕ АФФИЛИАЦИИ:")
    result.extend(top(counter_affiliations))
    result.append("\n")

    report_txt = "\n".join(result)

    if not save_to_txt:
        return report_txt

    output_path = os.path.join(
        tempfile.gettempdir(),
        f"excel_metadata_summary_{os.path.basename(excel_path)}.txt"
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report_txt)

    return output_path


if __name__ == "__main__":
    example_path = r"C:\Users\Aleksey\AppData\Local\Temp\citation_analysis_results_1763990289.xlsx"
    print(summarize_from_excel(example_path))
