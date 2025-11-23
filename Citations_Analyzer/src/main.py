import utils as  utils

import requests
import pandas as pd
from habanero import Crossref
import time
from typing import List, Dict, Any
from datetime import datetime
import re
from functools import lru_cache
import os
import tempfile
from tqdm import tqdm
from ratelimit import limits, sleep_and_retry
from tenacity import retry, stop_after_attempt, wait_exponential
import logging
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows
from concurrent.futures import ThreadPoolExecutor, as_completed


class Config:
    REQUEST_TIMEOUT = 30
    MAX_RETRIES = 3
    MAX_WORKERS = 15  # Количество одновременных потоков для запросов


class PerformanceMonitor:
    def __init__(self):
        self.start_time = None

    def start(self):
        self.start_time = datetime.now()

    def get_stats(self):
        if self.start_time:
            elapsed = (datetime.now() - self.start_time).total_seconds()
            return {'elapsed_seconds': elapsed}
        return {}


class CitationAnalyzer:
    def __init__(self):
        self.crossref_cache = {}
        self.openalex_cache = {}
        self.performance_monitor = PerformanceMonitor()
        self.setup_logging()

    def setup_logging(self):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(f'doi_analyzer_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)

    @sleep_and_retry
    @limits(calls=15, period=1)
    def get_openalex_data(self, doi: str) -> Dict:
        if doi in self.openalex_cache: return self.openalex_cache[doi]
        try:
            url = f"https://api.openalex.org/works/https://doi.org/{doi}"
            response = requests.get(url, timeout=Config.REQUEST_TIMEOUT)
            response.raise_for_status()
            self.openalex_cache[doi] = response.json()
            return self.openalex_cache[doi]
        except Exception:
            self.openalex_cache[doi] = {}
            return {}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=5))
    @sleep_and_retry
    @limits(calls=15, period=1)
    def get_crossref_data(self, doi: str) -> Dict:
        if doi in self.crossref_cache: return self.crossref_cache[doi]
        try:
            cr = Crossref()
            self.crossref_cache[doi] = cr.works(ids=doi)['message']
            return self.crossref_cache[doi]
        except Exception:
            self.crossref_cache[doi] = {}
            return {}

    @lru_cache(maxsize=1000)
    def extract_surname_with_initial(self, author_name: str) -> str:
        if not author_name or author_name in ['Unknown', 'Error']: return author_name
        clean_name = re.sub(r'[^\w\s\-\.]', ' ', author_name).strip()
        parts = clean_name.split()
        if not parts: return author_name
        surname = parts[-1]
        initial = parts[0][0].upper() if parts[0] else ''
        return f"{surname} {initial}." if initial else surname

    def get_journal_info(self, crossref_data: Dict) -> Dict:
        container_title = crossref_data.get('container-title', [])
        short_title = crossref_data.get('short-container-title', [])
        full_name = container_title[0] if container_title else (short_title[0] if short_title else 'Unknown')
        abbreviation = short_title[0] if short_title else (container_title[0] if container_title else 'Unknown')
        return {'full_name': full_name, 'abbreviation': abbreviation,
                'publisher': crossref_data.get('publisher', 'Unknown')}

    def get_affiliations_and_countries(self, openalex_data: Dict) -> tuple[List[str], str]:
        affiliations, countries = set(), set()
        for authorship in openalex_data.get('authorships', []):
            for institution in authorship.get('institutions', []):
                if name := institution.get('display_name'): affiliations.add(name)
                if code := institution.get('country_code'): countries.add(code.upper())
        return list(affiliations) or ['Unknown'], ';'.join(sorted(countries)) or 'Unknown'

    def get_combined_article_data(self, doi: str) -> Dict[str, Any]:
        try:
            crossref_data = self.get_crossref_data(doi)
            openalex_data = self.get_openalex_data(doi)

            title = openalex_data.get('title') or (
                crossref_data.get('title', [])[0] if crossref_data.get('title') else 'Unknown')
            year = openalex_data.get('publication_year', 'Unknown')

            authors = [auth.get('author', {}).get('display_name', 'Unknown') for auth in
                       openalex_data.get('authorships', [])]
            authors_with_initials = [self.extract_surname_with_initial(name) for name in authors]

            journal_info = self.get_journal_info(crossref_data)
            affiliations, countries = self.get_affiliations_and_countries(openalex_data)

            return {
                'DOI': doi,
                'Название статьи': title,
                'Авторы': ', '.join(authors) or 'Unknown',
                'Инициалы авторов': ', '.join(authors_with_initials) or 'Unknown',
                'Год публикации': year,
                'Сокращённое наименование журнала': journal_info['abbreviation'],
                'Полное наименование журнала': journal_info['full_name'],
                'Издатель': journal_info['publisher'],
                'Аффилиация': '; '.join(affiliations),
                'Страны': countries,
                'Цитирования (CrossRef)': crossref_data.get('is-referenced-by-count', 0),
                'Цитирования (OpenAlex)': openalex_data.get('cited_by_count', 0),
            }
        except Exception as e:
            return {'DOI': doi, 'Название статьи': 'Error', 'error': str(e)}

    def get_source_articles_data(self, doi_list: List[str]) -> pd.DataFrame:
        source_data = [self.get_combined_article_data(doi) for doi in doi_list]
        df = pd.DataFrame(source_data)
        df.replace('Unknown', '-', inplace=True)
        return df

    def get_citing_articles_from_openalex(self, doi: str) -> List[str]:
        citing_dois = []
        try:
            work_data = self.get_openalex_data(doi)
            work_id = work_data.get('id')
            if not work_id or work_data.get('cited_by_count', 0) == 0:
                return []

            citing_url = f"https://api.openalex.org/works?filter=cites:{work_id}&per-page=200&select=doi"
            while citing_url:
                response = requests.get(citing_url, timeout=Config.REQUEST_TIMEOUT)
                if response.status_code == 200:
                    data = response.json()
                    for work in data.get('results', []):
                        if work.get('doi'): citing_dois.append(utils.normalize_doi(work['doi']))
                    citing_url = data.get('meta', {}).get('next_cursor')
                    time.sleep(0.1)
                else:
                    break
        except Exception as e:
            self.logger.error(f"Error getting citing articles from OpenAlex for {doi}: {e}")
        return citing_dois

    def find_citing_articles(self, doi_list: List[str]) -> Dict[str, List[str]]:
        results = {}
        for i, doi in enumerate(doi_list, 1):
            print(f"🔍 [{i}/{len(doi_list)}] Поиск цитирований для: {doi}")
            citations = self.get_citing_articles_from_openalex(doi)
            results[doi] = list(set(citations))
            print(f"✅ Найдено цитирований: {len(results[doi])}")
        return results

    # ИЗМЕНЕННЫЙ МЕТОД, ВОЗВРАЩАЮЩИЙ СЛОВАРЬ DataFrame'ов
    def process_citing_articles_parallel(self, doi_list: List[str]) -> Dict[str, pd.DataFrame]:
        self.performance_monitor.start()
        print("🔍 Step 1: Поиск всех цитирующих статей...")
        # citing_results - это словарь {'source_doi': [list_of_citing_dois]}
        citing_results = self.find_citing_articles(doi_list)
        all_unique_citing_dois = {doi for doi_list in citing_results.values() for doi in doi_list}

        if not all_unique_citing_dois:
            print("Не найдено цитирующих статей.")
            return {doi: pd.DataFrame() for doi in doi_list}

        print(
            f"🔍 Step 2: Найдено {len(all_unique_citing_dois)} уникальных цитирующих статей. Запускаем параллельную обработку...")

        # Собираем данные по всем уникальным статьям в один кэш
        fetched_data_map = {}
        with ThreadPoolExecutor(max_workers=Config.MAX_WORKERS) as executor:
            future_to_doi = {executor.submit(self.get_combined_article_data, doi): doi for doi in
                             all_unique_citing_dois}
            for future in tqdm(as_completed(future_to_doi), total=len(all_unique_citing_dois),
                               desc="Обработка цитирующих статей"):
                try:
                    article_data = future.result()
                    # Ключ - DOI статьи, значение - её метаданные
                    fetched_data_map[article_data['DOI']] = article_data
                except Exception as e:
                    doi = future_to_doi[future]
                    fetched_data_map[doi] = {'DOI': doi, 'Название статьи': 'Error Processing', 'error': str(e)}

        # Создаем отдельный DataFrame для каждого исходного DOI
        final_dataframes_dict = {}
        for source_doi, citing_doi_list in citing_results.items():
            # Собираем данные только для тех статей, которые цитируют этот source_doi
            article_data_for_this_source = [fetched_data_map[doi] for doi in citing_doi_list if doi in fetched_data_map]

            df = pd.DataFrame(article_data_for_this_source)
            df.replace('Unknown', '-', inplace=True)
            final_dataframes_dict[source_doi] = df

        return final_dataframes_dict

    # ОБНОВЛЕННАЯ ФУНКЦИЯ СОХРАНЕНИЯ
    def save_citation_analysis_to_excel(self, source_articles_df: pd.DataFrame,
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


def analyze_citing_articles(doi_input_text: str):
    analyzer = CitationAnalyzer()
    doi_list = utils.parse_doi_input(doi_input_text)
    if not doi_list:
        return

    print("\nStarting analysis...")
    try:
        print("🔍 Fetching metadata for source article(s)...")
        source_articles_df = analyzer.get_source_articles_data(doi_list)

        # Теперь эта функция вернет словарь DataFrame'ов
        citing_dataframes_dict = analyzer.process_citing_articles_parallel(doi_list)

        # Общее количество найденных цитирующих статей для статистики
        total_citations_found = sum(len(df) for df in citing_dataframes_dict.values())

        if total_citations_found > 0:
            stats = analyzer.performance_monitor.get_stats()
            print(f"\n{'=' * 80}\nANALYSIS RESULTS\n{'=' * 80}")
            print(f"Total source articles analyzed: {len(doi_list)}")
            print(f"Total unique citing articles relationships found: {total_citations_found}")
            print(f"Total processing time: {stats.get('elapsed_seconds', 0):.2f} seconds")

            excel_name = analyzer.save_citation_analysis_to_excel(source_articles_df, citing_dataframes_dict)
            print(f"\nAnalysis saved to: {excel_name}")
        else:
            print("No citing articles found for any of the provided DOIs.")

    except Exception as e:
        print(f"A critical error occurred: {e}")
        analyzer.save_citation_analysis_to_excel(pd.DataFrame(), {})
        print("An error report has been generated.")


if __name__ == "__main__":
    # Пример с двумя DOI для демонстрации
    doi_input = """
    10.1038/s41586-023-06924-6
    10.1016/j.jalgebra.2016.05.025
    10.1126/science.adi1887
    """
    analyze_citing_articles(doi_input)