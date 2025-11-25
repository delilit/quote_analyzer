import os
from pyclbr import Class
import time
import tempfile
import pandas as pd
from typing import Dict
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows
import logging

class ExcelBuilder:

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    # ОБНОВЛЕННАЯ ФУНКЦИЯ СОХРАНЕНИЯ
    def save_excel(self, source_articles_df: pd.DataFrame,
                                        citing_dataframes_dict: Dict[str, pd.DataFrame]) -> str:
        try:
            timestamp = int(time.time())
            excel_path = os.path.join(tempfile.gettempdir(), f"citation_analysis_results_{timestamp}.xlsx")
            wb = Workbook()
            wb.remove(wb.active)

            # Создаем ПЕРВЫЙ лист с метаданными исходной статьи
            ws_source = wb.create_sheet("Сведения об указанных статьях", 0)
            if not source_articles_df.empty:
                for r in dataframe_to_rows(source_articles_df, index=False, header=True):
                    ws_source.append(r)
            else:
                ws_source.append(["Данные не найдены"])

            # В цикле создаем по одному листу для каждого исходного DOI
            for source_doi, citing_df in citing_dataframes_dict.items():
                # Создаем безопасное имя для листа, заменяя недопустимые символы
                safe_sheet_name = f"Цитирования {source_doi.replace('/', '_')}"
                safe_sheet_name = safe_sheet_name[:31]  # Ограничение Excel на длину имени листа

                ws = wb.create_sheet(safe_sheet_name)
                if not citing_df.empty:
                    for r in dataframe_to_rows(citing_df, index=False, header=True):
                        ws.append(r)
                else:
                    ws.append([f"Цитирований статьи с DOI: {source_doi} не найдено"])

            wb.save(excel_path)
            return excel_path
        except Exception as e:
            self.logger.critical(f"Critical error in save_citation_analysis_to_excel: {e}")
            return "error_creating_report"