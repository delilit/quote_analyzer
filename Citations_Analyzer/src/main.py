import api as api
import utils as  utils
import builder as builder

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
        #modules
        self.api = api.APIClient()
        self.builder = builder.ExcelBuilder()

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

    def get_combined_article_data(self, doi: str) -> Dict[str, Any]:
        try:
            crossref_data = self.api.get_crossref_data(doi)
            openalex_data = self.api.get_openalex_data(doi)

            title = openalex_data.get('title') or (
                crossref_data.get('title', [])[0] if crossref_data.get('title') else 'Unknown')
            year = openalex_data.get('publication_year', 'Unknown')

            authors = [auth.get('author', {}).get('display_name', 'Unknown') for auth in
                       openalex_data.get('authorships', [])]
            authors_with_initials = [utils.extract_surname_with_initial(name) for name in authors]

            journal_info = utils.get_journal_info(crossref_data)
            affiliations, countries = utils.get_affiliations_and_countries(openalex_data)

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
            work_data = self.api.get_openalex_data(doi)
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
#new function

    def get_cited_dois_for_source(self, source_doi: str) -> List[str]:
        """
        Возвращает список DOI, на которые ссылается статья source_doi (через Crossref references).
        Использует APIClient.get_references_from_crossref и utils.normalize_doi / validate_doi.
        """
        refs = []
        try:
            ref_list = self.api.get_references_from_crossref(source_doi)
            for r in ref_list:
                # Crossref reference может иметь ключи 'DOI', 'doi', 'doi-raw' и т.п.
                cand = None
                for key in ('DOI', 'doi', 'doi-raw', 'DOI-raw'):
                    if r.get(key):
                        cand = r.get(key)
                        break
                if not cand:
                    # иногда Crossref кладёт doi в поле 'unstructured' или 'article-title' - пропускаем
                    continue
                norm = utils.normalize_doi(str(cand))
                if norm and utils.validate_doi(norm):
                    refs.append(norm)
        except Exception as e:
            self.logger.error(f"Error fetching references for {source_doi}: {e}")
        return refs

    def process_cited_articles_parallel(self, doi_list: List[str]) -> Dict[str, pd.DataFrame]:
        """
        Для каждого DOI из doi_list собирает DOI'ы, на которые он ссылается,
        затем параллельно получает метаданные для всех уникальных ссылок и
        формирует словарь {source_doi: DataFrame(ссылки)}.
        Возвращает словарь DataFrame'ов (ключи — исходные DOI).
        """
        self.performance_monitor.start()
        print("🔍 Step 1: Сбор ссылок (references) для указанных DOI...")
        cited_map = {}  # source_doi -> list of referenced DOI
        for i, doi in enumerate(doi_list, 1):
            print(f"📥 [{i}/{len(doi_list)}] Получаем references для: {doi}")
            refs = self.get_cited_dois_for_source(doi)
            refs_unique = sorted(set(refs))
            cited_map[doi] = refs_unique
            print(f"   → Найдено ссылок: {len(refs_unique)}")

        all_referenced = {d for refs in cited_map.values() for d in refs}
        if not all_referenced:
            print("Не найдено ни одной ссылки в Crossref для предоставленных DOI.")
            return {doi: pd.DataFrame() for doi in doi_list}

        print(f"🔍 Step 2: Всего уникальных ссылок для обработки: {len(all_referenced)}. Собираем метаданные параллельно...")

        fetched_map = {}
        with ThreadPoolExecutor(max_workers=Config.MAX_WORKERS) as executor:
            future_to_doi = {executor.submit(self.get_combined_article_data, doi): doi for doi in all_referenced}
            for future in tqdm(as_completed(future_to_doi), total=len(future_to_doi), desc="Обработка ссылок"):
                src_doi = future_to_doi[future]
                try:
                    meta = future.result()
                    # meta должна содержать ключ 'DOI'
                    fetched_map[meta.get('DOI', src_doi)] = meta
                except Exception as e:
                    fetched_map[src_doi] = {'DOI': src_doi, 'Название статьи': 'Error', 'error': str(e)}

        # Для каждого исходного DOI собираем DataFrame его ссылок
        result_frames = {}
        for source, refs in cited_map.items():
            rows = [fetched_map[r] for r in refs if r in fetched_map]
            df = pd.DataFrame(rows)
            df.replace('Unknown', '-', inplace=True)
            result_frames[source] = df

        return result_frames
    
def analyze_cited_articles(doi_input_text: str):
    analyzer = CitationAnalyzer()
    doi_list = utils.parse_doi_input(doi_input_text)
    if not doi_list:
        return

    print("\nStarting OUTBOUND references analysis (DOIs that the provided DOIs cite)...")
    try:
        print("🔍 Fetching metadata for source article(s)...")
        source_articles_df = analyzer.get_source_articles_data(doi_list)

        # Новая функция вернёт словарь DataFrame'ов: для каждого исходного DOI — DataFrame его references
        cited_dataframes_dict = analyzer.process_cited_articles_parallel(doi_list)

        total_references_found = sum(len(df) for df in cited_dataframes_dict.values())

        if total_references_found > 0:
            stats = analyzer.performance_monitor.get_stats()
            print(f"\n{'=' * 80}\nANALYSIS RESULTS (OUTBOUND REFERENCES)\n{'=' * 80}")
            print(f"Total source articles analyzed: {len(doi_list)}")
            print(f"Total unique referenced articles found: {total_references_found}")
            print(f"Total processing time: {stats.get('elapsed_seconds', 0):.2f} seconds")

            excel_name = analyzer.builder.save_excel(source_articles_df, cited_dataframes_dict)
            print(f"\nAnalysis saved to: {excel_name}")
        else:
            print("No outbound references with valid DOIs found for any provided DOI.")

    except Exception as e:
        print(f"A critical error occurred: {e}")
        analyzer.builder.save_excel(pd.DataFrame(), {})
        print("An error report has been generated.")


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

            excel_name = analyzer.builder.save_excel(source_articles_df, citing_dataframes_dict)
            print(f"\nAnalysis saved to: {excel_name}")
        else:
            print("No citing articles found for any of the provided DOIs.")

    except Exception as e:
        print(f"A critical error occurred: {e}")
        analyzer.builder.save_excel(pd.DataFrame(), {})
        print("An error report has been generated.")

if __name__ == "__main__":
    # Пример с двумя DOI для демонстрации
    doi_input = """
    10.1038/s41586-023-06924-6
    10.1016/j.jalgebra.2016.05.025
    10.1126/science.adi1887
    """
    #analyze_citing_articles(doi_input)
    analyze_cited_articles(doi_input)
    