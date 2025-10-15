#!pip install habanero crossref-commons requests pandas tqdm ipywidgets google-colab ratelimit tenacity beautifulsoup4 --quiet

import requests
import json
import pandas as pd
from habanero import Crossref
from crossref_commons.retrieval import get_publication_as_json
import time
from typing import List, Dict, Any
from datetime import datetime
from collections import Counter
import re
import numpy as np
from functools import lru_cache
import os
import tempfile
import zipfile
import shutil
from google.colab import files
from tqdm.notebook import tqdm
from contextlib import redirect_stdout
from io import StringIO
import ipywidgets as widgets
from IPython.display import display, clear_output
from concurrent.futures import ThreadPoolExecutor, as_completed
from ratelimit import limits, sleep_and_retry
from tenacity import retry, stop_after_attempt, wait_exponential
import logging
from bs4 import BeautifulSoup

class Config:
    REQUEST_TIMEOUT = 5
    MAX_WORKERS = 10

class PerformanceMonitor:
    def __init__(self):
        self.start_time = None
        self.request_count = 0

    def start(self):
        self.start_time = datetime.now()

    def increment_request(self):
        self.request_count += 1

    def get_stats(self):
        if self.start_time:
            elapsed = (datetime.now() - self.start_time).total_seconds()
            return {
                'total_requests': self.request_count,
                'elapsed_seconds': elapsed,
                'elapsed_minutes': elapsed / 60,
                'requests_per_second': self.request_count / elapsed if elapsed > 0 else 0
            }
        return {}

class CitationAnalyzer:
    def __init__(self, rate_limit_calls=10, rate_limit_period=1):
        self.crossref_cache = {}
        self.openalex_cache = {}
        self.rate_limit_calls = rate_limit_calls
        self.rate_limit_period = rate_limit_period
        self.performance_monitor = PerformanceMonitor()
        self._unique_references_cache = {}
        self.unique_ref_data_cache = {}  # Кэш для данных уникальных ссылок
        self.setup_logging()

    def setup_logging(self): # Ligging set-up.
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', #Set up a logging format. It's time when log was made, name, level (INFO), and messege by itself.
            handlers=[
                logging.FileHandler(f'citation_analyzer_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'), #Creating a log file with unique name (date of made).
                logging.StreamHandler() #Into terminal
            ]
        )
        self.logger = logging.getLogger(__name__) #Creating logger object.

    def validate_doi(self, doi: str) -> bool: #refactored
        """Проверка валидности DOI"""
        if not doi or not isinstance(doi, str):
            self.logger.debug(f"Invalid DOI: {doi} (empty or not a string)")
            return False
        doi_pattern = r'^10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+$'
        is_valid = bool(re.match(doi_pattern, doi, re.IGNORECASE))
        if not is_valid:
            self.logger.debug(f"DOI {doi} does not match pattern")
        return is_valid

    def normalize_doi(self, doi: str) -> str: #refactored
        """Нормализация DOI"""
        doi = doi.strip()
        prefixes = ['https://doi.org/', 'doi:', 'http://doi.org/']
        for prefix in prefixes:
            if doi.lower().startswith(prefix):
                doi = doi[len(prefix):]
        normalized_doi = doi.lower()
        self.logger.debug(f"Normalized DOI: {doi} -> {normalized_doi}")
        return normalized_doi

    @sleep_and_retry #it's making limits for REST API calls.
    @limits(calls=10, period=1) # 10 calls in 1 second is limit.
    def get_openalex_data(self, doi: str) -> Dict:
        """Кэшируем запросы к OpenAlex с улучшенной обработкой ошибок"""
        if doi in self.openalex_cache:
            self.logger.debug(f"OpenAlex cache hit for DOI: {doi}") #Any kind of "logger.debug() strings means new logger messege"
            return self.openalex_cache[doi]

        try:
            openalex_url = f"https://api.openalex.org/works/https://doi.org/{doi}"
            response = requests.get(openalex_url, timeout=Config.REQUEST_TIMEOUT)
            self.performance_monitor.increment_request()

            if response.status_code == 404:
                self.logger.warning(f"DOI {doi} not found in OpenAlex")
                self.openalex_cache[doi] = {}
                return {}

            response.raise_for_status()
            result = response.json()
            self.openalex_cache[doi] = result
            self.logger.debug(f"OpenAlex data fetched for DOI: {doi}")
            return result

        except requests.exceptions.Timeout:
            self.logger.error(f"OpenAlex timeout for DOI {doi}")
        except requests.exceptions.RequestException as e:
            self.logger.error(f"OpenAlex request error for DOI {doi}: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error for DOI {doi}: {e}")

        self.openalex_cache[doi] = {}
        return {}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    @sleep_and_retry
    @limits(calls=10, period=1)
    def get_crossref_data(self, doi: str) -> Dict:
        """Кэшируем запросы к Crossref с повторными попытками, извлекая год из published-print или published-online"""
        if doi in self.crossref_cache:
            self.logger.debug(f"Crossref cache hit for DOI: {doi}")
            return self.crossref_cache[doi]
        try:
            cr = Crossref()
            result = cr.works(ids=doi)
            self.performance_monitor.increment_request()
            data = result['message']

            # Извлечение года публикации из published-print или published-online
            year = None
            if 'published-print' in data and 'date-parts' in data['published-print']:
                date_parts = data['published-print']['date-parts'][0]
                year = date_parts[0] if date_parts else None
            elif 'published-online' in data and 'date-parts' in data['published-online']:
                date_parts = data['published-online']['date-parts'][0]
                year = date_parts[0] if date_parts else None
            elif 'issued' in data and 'date-parts' in data['issued']:
                date_parts = data['issued']['date-parts'][0]
                year = date_parts[0] if date_parts else None

            data['publication_year'] = year if year else 'Unknown'
            self.crossref_cache[doi] = data
            self.logger.debug(f"Crossref data fetched for DOI: {doi}, publication year: {data['publication_year']}")
            return data
        except Exception as e:
            self.crossref_cache[doi] = {'publication_year': 'Unknown'}
            self.logger.error(f"Crossref error for DOI {doi}: {e}")
            return {'publication_year': 'Unknown'}

    @sleep_and_retry
    @limits(calls=10, period=1)
    def search_doi_via_crossref(self, title: str) -> str:
        """Поиск DOI через Crossref API"""
        if not title or title == 'Unknown':
            self.logger.debug(f"Cannot search for DOI with title: {title}")
            return None

        url = "https://api.crossref.org/works"
        params = {'query.title': title, 'rows': 1}
        headers = {'User-Agent': 'CitationAnalyzer/1.0 (mailto:your@email.com)'}

        try:
            response = requests.get(url, params=params, headers=headers, timeout=Config.REQUEST_TIMEOUT)
            self.performance_monitor.increment_request()
            response.raise_for_status()
            data = response.json()

            if data['message']['total-results'] > 0:
                item = data['message']['items'][0]
                doi = item.get('DOI')
                if self.validate_doi(doi):
                    self.logger.info(f"Found DOI {doi} for title '{title}' in Crossref")
                    return doi
            self.logger.debug(f"No DOI found for title '{title}' in Crossref")
            return None

        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error searching title '{title}' in Crossref: {e}")
            return None

    @sleep_and_retry
    @limits(calls=10, period=1)
    def search_doi_by_google_scholar(self, title: str) -> str:
        """Поиск DOI через Google Scholar"""
        if not title or title == 'Unknown':
            self.logger.debug(f"Cannot search for DOI with title: {title}")
            return None

        try:
            search_query = requests.utils.quote(title)
            url = f"https://scholar.google.com/scholar?q={search_query}"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            response = requests.get(url, headers=headers, timeout=Config.REQUEST_TIMEOUT)
            self.performance_monitor.increment_request()
            response.raise_for_status()

            soup = BeautifulSoup(response.text, 'html.parser')
            for link in soup.find_all('a', href=True):
                href = link['href']
                if 'doi.org' in href:
                    doi_match = re.search(r'(10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+)', href)
                    if doi_match:
                        doi = doi_match.group(1)
                        if self.validate_doi(doi):
                            self.logger.info(f"Found DOI {doi} for title '{title}' in Google Scholar")
                            return doi
            self.logger.debug(f"No DOI found for title '{title}' in Google Scholar")
            return None

        except Exception as e:
            self.logger.error(f"Error searching title '{title}' in Google Scholar: {e}")
            return None

    @sleep_and_retry
    @limits(calls=10, period=1)
    def search_doi_by_openalex(self, title: str) -> str:
        """Поиск DOI через OpenAlex API"""
        if not title or title == 'Unknown':
            self.logger.debug(f"Cannot search for DOI with title: {title}")
            return None

        try:
            openalex_url = f"https://api.openalex.org/works?filter=title.search:{requests.utils.quote(title)}"
            response = requests.get(openalex_url, timeout=Config.REQUEST_TIMEOUT)
            self.performance_monitor.increment_request()
            response.raise_for_status()
            results = response.json().get('results', [])

            for result in results:
                result_title = result.get('title', '').lower()
                if result_title and (title.lower() in result_title or result_title in title.lower()):
                    doi = result.get('doi', '').replace('https://doi.org/', '')
                    if self.validate_doi(doi):
                        self.logger.info(f"Found DOI {doi} for title '{title}' in OpenAlex")
                        return doi
            self.logger.debug(f"No DOI found for title '{title}' in OpenAlex")
            return None

        except Exception as e:
            self.logger.error(f"Error searching title '{title}' in OpenAlex: {e}")
            return None

    def find_doi_by_title(self, title: str) -> str:
        """Комбинированный поиск DOI: сначала через Crossref, затем через Google Scholar, затем через OpenAlex"""
        if not title or title == 'Unknown':
            self.logger.debug(f"Cannot search for DOI with title: {title}")
            return None

        self.logger.info(f"Searching DOI for title: {title}")

        self.logger.info("Attempting search via Crossref API...")
        doi = self.search_doi_via_crossref(title)
        if doi:
            self.logger.info(f"Found via Crossref: {doi}")
            return doi

        time.sleep(1)

        self.logger.info("Crossref failed, attempting Google Scholar...")
        doi = self.search_doi_by_google_scholar(title)
        if doi:
            self.logger.info(f"Found via Google Scholar: {doi}")
            return doi

        time.sleep(1)

        self.logger.info("Google Scholar failed, attempting OpenAlex...")
        doi = self.search_doi_by_openalex(title)

        if doi:
            self.logger.info(f"Found via OpenAlex: {doi}")
        else:
            self.logger.warning(f"DOI not found for title: {title}")

        return doi

    @sleep_and_retry
    @limits(calls=10, period=1)
    def quick_doi_search(self, title: str) -> str:
        """Быстрый поиск DOI по названию через Crossref"""
        if not title or title == 'Unknown':
            self.logger.debug(f"Cannot search for DOI with title: {title}")
            return None

        url = "https://api.crossref.org/works"
        params = {
            'query.title': title,
            'rows': 1,
            'select': 'DOI,title'
        }
        headers = {'User-Agent': 'CitationAnalyzer/1.0 (mailto:your@email.com)'}

        try:
            response = requests.get(url, params=params, headers=headers, timeout=Config.REQUEST_TIMEOUT)
            self.performance_monitor.increment_request()
            response.raise_for_status()
            data = response.json()

            if data['message']['items']:
                doi = data['message']['items'][0]['DOI']
                if self.validate_doi(doi):
                    self.logger.info(f"Found DOI {doi} for title '{title}' in quick_doi_search")
                    return doi
            self.logger.debug(f"No DOI found for title '{title}' in quick_doi_search")
            return None
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error searching title '{title}' in quick_doi_search: {e}")
            return None

    def process_failed_references(self, failed_references_path: str, temp_dir: str) -> str:
        """Обработка файла failed_citations.csv для поиска DOI по названиям статей"""
        timestamp = int(time.time())
        output_path = os.path.join(temp_dir, f"updated_failed_citations_{timestamp}.csv")

        try:
            failed_df = pd.read_csv(failed_references_path, encoding='utf-8')
            self.logger.info(f"Loaded failed_citations.csv with {len(failed_df)} entries")

            required_columns = ['source_doi', 'ref_number', 'reference_doi', 'error_description']
            if not all(col in failed_df.columns for col in required_columns):
                self.logger.error("Invalid failed_citations.csv format: missing required columns")
                return None

            failed_df['updated_doi'] = failed_df['reference_doi']
            failed_df['updated_error'] = failed_df['error_description']

            for index, row in tqdm(failed_df.iterrows(), total=len(failed_df), desc="Processing failed citations"):
                if pd.isna(row['reference_doi']) or not self.validate_doi(row['reference_doi']):
                    title_match = re.search(r"title '([^']+)'|title ([^,]+)", row['error_description'])
                    if title_match:
                        title = next((g for g in title_match.groups() if g), None).strip()
                        self.logger.info(f"Searching DOI for title: {title}")

                        doi = self.quick_doi_search(title)

                        if doi and self.validate_doi(doi):
                            failed_df.at[index, 'updated_doi'] = doi
                            failed_df.at[index, 'updated_error'] = f"DOI found: {doi}"
                            self.logger.info(f"Found DOI {doi} for title '{title}'")
                        else:
                            failed_df.at[index, 'updated_doi'] = None
                            failed_df.at[index, 'updated_error'] = f"No DOI found for title '{title}'"
                            self.logger.warning(f"No DOI found for title '{title}'")
                    else:
                        self.logger.warning(f"No title found in error_description for index {index}")
                        failed_df.at[index, 'updated_doi'] = None
                        failed_df.at[index, 'updated_error'] = "No title extracted from error_description"

            failed_df.to_csv(output_path, index=False, encoding='utf-8')
            self.logger.info(f"Updated failed citations saved to {output_path}")
            return output_path

        except FileNotFoundError:
            self.logger.error(f"File {failed_references_path} not found")
            return None
        except Exception as e:
            self.logger.error(f"Error processing failed_citations.csv: {e}")
            return None

    @lru_cache(maxsize=1000)
    def extract_surname_with_initial(self, author_name: str) -> str:
        """Извлечение фамилии с инициалом в формате 'Surname I.'"""
        if not author_name or author_name in ['Unknown', 'Error']:
            return author_name
        clean_name = re.sub(r'[^\w\s\-\.]', ' ', author_name).strip()
        parts = clean_name.split()
        if not parts:
            return author_name
        surname = parts[-1]
        initial = parts[0][0].upper() if parts[0] else ''
        return f"{surname} {initial}." if initial else surname

    def get_affiliations_and_countries_from_openalex(self, doi: str) -> tuple[List[str], str]:
        """Получение аффилиаций и стран из OpenAlex"""
        data = self.get_openalex_data(doi)
        affiliations = set()
        countries = set()
        for authorship in data.get('authorships', []):
            for institution in authorship.get('institutions', []):
                if name := institution.get('display_name'):
                    affiliations.add(name)
                if country_code := institution.get('country_code'):
                    formatted_code = country_code.upper()
                    countries.add(formatted_code)
        affiliations = list(affiliations) or ['Unknown']
        countries_str = ';'.join(sorted(countries)) if countries else 'Unknown'
        return affiliations, countries_str

    def get_citation_data(self, doi: str) -> tuple:
        """Получение данных о цитированиях"""
        data = self.get_crossref_data(doi)
        crossref_citations = data.get('is-referenced-by-count', 0)
        data = self.get_openalex_data(doi)
        openalex_citations = data.get('cited_by_count', 0)
        return doi, crossref_citations, openalex_citations

    def get_journal_info_from_crossref(self, doi: str) -> Dict[str, Any]:
        """Получение информации о журнале из Crossref"""
        data = self.get_crossref_data(doi)
        container_title = data.get('container-title', [])
        short_container_title = data.get('short-container-title', [])
        full_name = container_title[0] if container_title else (short_container_title[0] if short_container_title else 'Unknown')
        abbreviation = short_container_title[0] if short_container_title else (container_title[0] if container_title else 'Unknown')
        return {
            'full_name': full_name,
            'abbreviation': abbreviation,
            'publisher': data.get('publisher', 'Unknown'),
            'issn': data.get('ISSN', [None])[0]
        }

    def calculate_years_since_publication(self, publication_year: Any, current_year: int = None) -> int:
        """Рассчитывает количество лет с момента публикации"""
        if current_year is None:
            current_year = datetime.now().year
        try:
            year = int(publication_year)
            if 1900 < year <= current_year:
                return max(2 if year == 2024 else 1, current_year - year)
        except:
            return 1

    def calculate_annual_citation_rate(self, citation_count: int, publication_year: Any, current_year: int = None) -> float:
        """Рассчитывает ежегодную цитируемость"""
        if not isinstance(citation_count, (int, float)) or citation_count == 0:
            return 0.0
        return round(citation_count / self.calculate_years_since_publication(publication_year, current_year), 2)

    def get_citing_articles_openalex(self, doi: str) -> List[Dict]:
        """Получить полный список статей, которые цитируют указанную статью через OpenAlex"""
        citing_articles = []
        normalized_doi = self.normalize_doi(doi)
        openalex_url = f"https://api.openalex.org/works/https://doi.org/{normalized_doi}"

        try:
            self.logger.info(f"Fetching citing articles for DOI: {doi}")
            response = requests.get(openalex_url, timeout=Config.REQUEST_TIMEOUT)
            self.performance_monitor.increment_request()

            if response.status_code == 404:
                self.logger.warning(f"DOI {doi} not found in OpenAlex")
                return []

            response.raise_for_status()
            work_data = response.json()
            cited_by_count = work_data.get('cited_by_count', 0)
            self.logger.info(f"Found {cited_by_count} citations for DOI: {doi}")

            if cited_by_count == 0:
                self.logger.info(f"No citations found for DOI {doi}")
                return []

            cited_by_url = f"https://api.openalex.org/works?filter=cites:{work_data['id']}&per-page=100"
            page = 1
            cursor = '*'  # Начальный курсор для пагинации
            total_pages = (cited_by_count // 100) + 1 if cited_by_count % 100 != 0 else cited_by_count // 100

            with tqdm(total=total_pages, desc=f"Fetching pages for {doi}", leave=False) as page_pbar:
                while cited_by_url:
                    try:
                        # Используем cursor-based пагинацию
                        url_with_cursor = f"{cited_by_url}&cursor={cursor}"
                        response_cited = requests.get(url_with_cursor, timeout=Config.REQUEST_TIMEOUT)
                        self.performance_monitor.increment_request()
                        response_cited.raise_for_status()
                        cited_data = response_cited.json()
                        results = cited_data.get('results', [])
                        self.logger.info(f"Page {page}: Loaded {len(results)} citing articles for DOI {doi}")

                        for i, work in enumerate(results, start=(page-1)*100+1):
                            citing_doi = work.get('doi', '').replace('https://doi.org/', '') if work.get('doi') else None
                            if not citing_doi:
                                citing_doi = work.get('ids', {}).get('doi', '').replace('https://doi.org/', '')
                            if citing_doi and self.validate_doi(citing_doi):
                                citing_articles.append({
                                    'DOI': citing_doi,
                                    'article-title': work.get('title', 'Unknown'),
                                    'year': str(work.get('publication_year', 'Unknown')),
                                    'position': i
                                })

                        cursor = cited_data.get('meta', {}).get('next_cursor')
                        page += 1
                        page_pbar.update(1)
                        if not cursor:  # Если нет следующего курсора, прерываем цикл
                            self.logger.info(f"No more pages to fetch for DOI {doi}")
                            break
                        time.sleep(1.5)  # Задержка между запросами для соблюдения лимитов API
                    except Exception as e:
                        self.logger.error(f"Error loading page {page} for DOI {doi}: {e}")
                        break

            self.logger.info(f"Total citing articles for DOI {doi}: {len(citing_articles)}")
            return citing_articles

        except Exception as e:
            self.logger.error(f"Error fetching citing articles for DOI {doi}: {e}")
            return []

    def process_source_article(self, doi: str, current_year: int) -> Dict[str, Any]:
        """Обработка исходной статьи с использованием года из published-print или published-online"""
        try:
            title = 'Unknown'
            year = 'Unknown'
            publication_year = None
            journal_info = self.get_journal_info_from_crossref(doi)
            _, crossref_citations, openalex_citations = self.get_citation_data(doi)

            openalex_data = self.get_openalex_data(doi)
            if openalex_data:
                title = openalex_data.get('title', title)
                if openalex_year := openalex_data.get('publication_year'):
                    publication_year = openalex_year
                    year = str(openalex_year)
            else:
                crossref_data = self.get_crossref_data(doi)
                if crossref_data.get('publication_year') != 'Unknown':
                    publication_year = crossref_data['publication_year']
                    year = str(publication_year)

            authors = []
            authors_surnames = []
            authors_with_initials = []
            if openalex_data:
                for author in openalex_data.get('authorships', []):
                    name = author.get('author', {}).get('display_name', 'Unknown')
                    if name != 'Unknown':
                        authors.append(name)
                        surname_with_initial = self.extract_surname_with_initial(name)
                        authors_surnames.append(surname_with_initial)
                        authors_with_initials.append(surname_with_initial)

            authors = ', '.join(authors) if authors else 'Unknown'
            authors_surnames = ', '.join(authors_surnames) if authors_surnames else 'Unknown'
            authors_with_initials = ', '.join(authors_with_initials) if authors_with_initials else 'Unknown'
            affiliations, countries = self.get_affiliations_and_countries_from_openalex(doi)

            years_since_pub = self.calculate_years_since_publication(publication_year, current_year)

            source_info = {
                'source_doi': doi,
                'position': None,
                'doi': doi,
                'title': title,
                'authors': authors,
                'authors_surnames': authors_surnames,
                'authors_with_initials': authors_with_initials,
                'year': year,
                'journal_full_name': journal_info['full_name'],
                'journal_abbreviation': journal_info['abbreviation'],
                'publisher': journal_info['publisher'],
                'citation_count_crossref': crossref_citations,
                'citation_count_openalex': openalex_citations,
                'annual_citation_rate_crossref': self.calculate_annual_citation_rate(crossref_citations, publication_year, current_year),
                'annual_citation_rate_openalex': self.calculate_annual_citation_rate(openalex_citations, publication_year, current_year),
                'years_since_publication': years_since_pub,
                'affiliations': '; '.join(affiliations),
                'countries': countries,
                'error': None
            }
            self.logger.debug(f"Processed source article DOI: {doi}, year: {year}")
            return source_info
        except Exception as e:
            source_info = {
                'source_doi': doi,
                'position': None,
                'doi': doi,
                'title': 'Unknown',
                'authors': 'Error',
                'authors_surnames': 'Error',
                'authors_with_initials': 'Error',
                'year': 'Unknown',
                'journal_full_name': 'Error',
                'journal_abbreviation': 'Error',
                'publisher': 'Error',
                'citation_count_crossref': 'N/A',
                'citation_count_openalex': 'N/A',
                'annual_citation_rate_crossref': 'N/A',
                'annual_citation_rate_openalex': 'N/A',
                'years_since_publication': 'N/A',
                'affiliations': 'Error',
                'countries': 'Error',
                'error': str(e)
            }
            self.logger.error(f"Error processing source article {doi}: {e}")
            return source_info

    def process_citation(self, doi: str, citation: Dict[str, Any], position: int, source_doi: str, current_year: int, citation_pbar) -> tuple:
        """Обработка одной цитирующей статьи с использованием кэша и года из published-print или published-online"""
        if doi in self.unique_ref_data_cache:
            self.logger.debug(f"Using cached data for DOI: {doi}")
            citation_info = self.unique_ref_data_cache[doi].copy()
            citation_info['source_doi'] = source_doi
            citation_info['position'] = position
            journal_abbr = citation_info.get('journal_abbreviation', 'Unknown')
            year = citation_info.get('year', 'Unknown')
            crossref_citations = citation_info.get('citation_count_crossref', 'N/A')
            openalex_citations = citation_info.get('citation_count_openalex', 'N/A')
            message = f"Citation {position}: {journal_abbr} ({year}) - {crossref_citations}x (Crossref), {openalex_citations}x (OpenAlex)"
            citation_pbar.update(1)
            return citation_info, message

        try:
            title = citation.get('article-title', 'Unknown')
            year = citation.get('year', 'Unknown')
            publication_year = None

            journal_info = self.get_journal_info_from_crossref(doi)
            _, crossref_citations, openalex_citations = self.get_citation_data(doi)

            openalex_data = self.get_openalex_data(doi)
            if openalex_data:
                title = openalex_data.get('title', title)
                if openalex_year := openalex_data.get('publication_year'):
                    publication_year = openalex_year
                    year = str(openalex_year)
            else:
                crossref_data = self.get_crossref_data(doi)
                if crossref_data.get('publication_year') != 'Unknown':
                    publication_year = crossref_data['publication_year']
                    year = str(publication_year)

            authors = []
            authors_surnames = []
            authors_with_initials = []
            if openalex_data:
                for author in openalex_data.get('authorships', []):
                    name = author.get('author', {}).get('display_name', 'Unknown')
                    if name != 'Unknown':
                        authors.append(name)
                        surname_with_initial = self.extract_surname_with_initial(name)
                        authors_surnames.append(surname_with_initial)
                        authors_with_initials.append(surname_with_initial)

            authors = ', '.join(authors) if authors else 'Unknown'
            authors_surnames = ', '.join(authors_surnames) if authors_surnames else 'Unknown'
            authors_with_initials = ', '.join(authors_with_initials) if authors_with_initials else 'Unknown'
            affiliations, countries = self.get_affiliations_and_countries_from_openalex(doi)

            years_since_pub = self.calculate_years_since_publication(publication_year, current_year)

            citation_info = {
                'source_doi': source_doi, 'position': position, 'doi': doi,
                'title': title, 'authors': authors, 'authors_surnames': authors_surnames,
                'authors_with_initials': authors_with_initials, 'year': year,
                'journal_full_name': journal_info['full_name'],
                'journal_abbreviation': journal_info['abbreviation'],
                'publisher': journal_info['publisher'],
                'citation_count_crossref': crossref_citations,
                'citation_count_openalex': openalex_citations,
                'annual_citation_rate_crossref': self.calculate_annual_citation_rate(crossref_citations, publication_year, current_year),
                'annual_citation_rate_openalex': self.calculate_annual_citation_rate(openalex_citations, publication_year, current_year),
                'years_since_publication': years_since_pub,
                'affiliations': '; '.join(affiliations),
                'countries': countries,
                'error': None
            }
            self.unique_ref_data_cache[doi] = citation_info
            message = f"Citation {position}: {journal_info['abbreviation']} ({year}) - {crossref_citations}x (Crossref), {openalex_citations}x (OpenAlex)"
            citation_pbar.update(1)
            return citation_info, message
        except Exception as e:
            citation_info = {
                'source_doi': source_doi, 'position': position, 'doi': doi,
                'title': citation.get('article-title', 'Unknown'),
                'authors': 'Error', 'authors_surnames': 'Error', 'authors_with_initials': 'Error',
                'year': 'Unknown', 'journal_full_name': 'Error', 'journal_abbreviation': 'Error',
                'publisher': 'Error', 'citation_count_crossref': 'N/A',
                'citation_count_openalex': 'N/A',
                'annual_citation_rate_crossref': 'N/A',
                'annual_citation_rate_openalex': 'N/A',
                'years_since_publication': 'N/A',
                'affiliations': 'Error', 'countries': 'Error', 'error': str(e)
            }
            self.unique_ref_data_cache[doi] = citation_info
            citation_pbar.update(1)
            return citation_info, f"Citation {position}: Error - {str(e)}"

    def collect_all_citations(self, doi_list: List[str]) -> tuple[List[Dict], set]:
        """Сбор всех DOI цитирующих статей из списка исходных статей"""
        all_citations = []
        unique_dois = set()
        for doi in tqdm(doi_list, desc="Collecting all citing DOIs"):
            try:
                citations = self.get_citing_articles_openalex(doi)
                for citation in citations:
                    citation_doi = citation.get('DOI')
                    if citation_doi and self.validate_doi(citation_doi):
                        unique_dois.add(citation_doi)
                    all_citations.append({'source_doi': doi, 'position': citation['position'], 'citation': citation})
            except Exception as e:
                self.logger.error(f"Error collecting citations for {doi}: {e}")
        self.logger.info(f"Total citations: {len(all_citations)}, Unique DOIs: {len(unique_dois)}")
        return all_citations, unique_dois

    def process_citation_wrapper(self, citation: Dict[str, Any], position: int, source_doi: str, current_year: int, citation_pbar) -> tuple:
        """Обертка для обработки цитирующей статьи с учетом поиска DOI по заголовку"""
        citation_doi = citation.get('DOI')
        title = citation.get('article-title', 'Unknown')

        if citation_doi and self.validate_doi(citation_doi):
            return self.process_citation(citation_doi, citation, position, source_doi, current_year, citation_pbar)
        else:
            self.logger.warning(f"No valid DOI found for citation {position} in {source_doi}: {citation_doi}")
            if title != 'Unknown':
                found_doi = self.find_doi_by_title(title)
                if found_doi:
                    self.logger.info(f"Found DOI {found_doi} for title '{title}'")
                    citation['DOI'] = found_doi
                    return self.process_citation(found_doi, citation, position, source_doi, current_year, citation_pbar)

            citation_info = {
                'source_doi': source_doi, 'position': position, 'doi': None,
                'title': title,
                'authors': 'Unknown', 'authors_surnames': 'Unknown', 'authors_with_initials': 'Unknown',
                'year': citation.get('year', 'Unknown'), 'journal_full_name': 'Unknown',
                'journal_abbreviation': 'Unknown', 'publisher': 'Unknown',
                'citation_count_crossref': 'N/A', 'citation_count_openalex': 'N/A',
                'annual_citation_rate_crossref': 'N/A', 'annual_citation_rate_openalex': 'N/A',
                'years_since_publication': 'N/A', 'affiliations': 'Unknown',
                'countries': 'Unknown', 'error': f"Invalid or missing DOI: {citation_doi}, no match found for title '{title}'"
            }
            citation_pbar.update(1)
            return citation_info, f"Citation {position}: No valid DOI found for {source_doi}, title '{title}'"

    def process_dois_parallel(self, doi_list: List[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Параллельная обработка списка DOI с предварительным сбором всех цитирующих DOI и учетом дубликатов"""
        self.performance_monitor.start()
        self.logger.info("Collecting all citing DOIs from input articles...")

        # Шаг 1: Сбор всех цитирующих статей для всех входных DOI
        all_citations, unique_dois = self.collect_all_citations(doi_list)
        total_citations = len(all_citations)
        print(f"\nTotal citations found: {total_citations}")
        print(f"Unique DOIs: {len(unique_dois)}")

        results = []
        source_articles = []
        current_year = datetime.now().year

        # Шаг 2: Обработка исходных статей
        self.logger.info("Processing source articles...")
        with tqdm(total=len(doi_list), desc="Processing source articles", position=0) as source_pbar:
            with ThreadPoolExecutor(max_workers=Config.MAX_WORKERS) as executor:
                future_to_doi = {
                    executor.submit(self.process_source_article, doi, current_year): doi
                    for doi in doi_list
                }
                for future in as_completed(future_to_doi):
                    doi = future_to_doi[future]
                    try:
                        source_info = future.result()
                        source_articles.append(source_info)
                        source_pbar.update(1)
                        clear_output(wait=True)
                        display(source_pbar.container)
                    except Exception as e:
                        self.logger.error(f"Error processing source DOI {doi}: {e}")
                        print(f"Error processing source DOI {doi}: {e}")
                        source_pbar.update(1)

        # Шаг 3: Обработка всех цитирующих статей (включая дубликаты)
        self.logger.info("Processing all citing articles...")
        article_citations = []
        with tqdm(total=len(all_citations), desc="Processing all citations", position=0) as citation_pbar:
            with ThreadPoolExecutor(max_workers=Config.MAX_WORKERS) as executor:
                future_to_cit = {
                    executor.submit(self.process_citation_wrapper, cit_data['citation'], cit_data['position'], cit_data['source_doi'], current_year, citation_pbar): cit_data
                    for cit_data in all_citations
                }
                for future in as_completed(future_to_cit):
                    try:
                        cit_info, message = future.result()
                        article_citations.append(cit_info)
                        print(message)
                    except Exception as e:
                        cit_data = future_to_cit[future]
                        position = cit_data['position']
                        source_doi = cit_data['source_doi']
                        self.logger.error(f"Error processing citation {position} for DOI {source_doi}: {e}")
                        print(f"Citation {position} for {source_doi}: Error - {str(e)}")
                        citation_pbar.update(1)

        combined_citations_df = pd.DataFrame(article_citations)
        source_articles_df = pd.DataFrame(source_articles)

        # Шаг 4: Логирование статистики
        self.logger.info(f"Processed {len(source_articles)} source articles and {len(article_citations)} citations")
        return combined_citations_df, source_articles_df

    def get_unique_citations(self, citations_df: pd.DataFrame) -> pd.DataFrame:
        """Получение уникальных цитирующих статей с кэшированием"""
        cache_key = id(citations_df)
        if cache_key not in self._unique_references_cache:
            citations_df['cit_id'] = citations_df['doi'].fillna('') + '|' + citations_df['title'].fillna('')
            unique_df = citations_df.drop_duplicates(subset=['cit_id'], keep='first').drop(columns=['cit_id'])
            self._unique_references_cache[cache_key] = unique_df
        return self._unique_references_cache[cache_key]

    def analyze_authors_frequency(self, citations_df: pd.DataFrame) -> pd.DataFrame:
        """Анализ частоты авторов"""
        total_cits = len(citations_df)
        unique_df = self.get_unique_citations(citations_df)
        total_unique = len(unique_df)

        authors_total = citations_df['authors_with_initials'].str.split(',', expand=True).stack()
        authors_total = authors_total[authors_total.str.strip().isin(['Unknown', 'Error']) == False]
        author_freq_total = authors_total.value_counts().reset_index()
        author_freq_total.columns = ['author_with_initial', 'frequency_total']
        author_freq_total['percentage_total'] = (author_freq_total['frequency_total'] / total_cits * 100).round(2)

        authors_unique = unique_df['authors_with_initials'].str.split(',', expand=True).stack()
        authors_unique = authors_unique[authors_unique.str.strip().isin(['Unknown', 'Error']) == False]
        author_freq_unique = authors_unique.value_counts().reset_index()
        author_freq_unique.columns = ['author_with_initial', 'frequency_unique']
        author_freq_unique['percentage_unique'] = (author_freq_unique['frequency_unique'] / total_unique * 100).round(2)

        author_freq = author_freq_total.merge(author_freq_unique, on='author_with_initial', how='outer').fillna(0)
        return author_freq[['author_with_initial', 'frequency_total', 'percentage_total', 'frequency_unique', 'percentage_unique']].sort_values('frequency_total', ascending=False)

    def analyze_journals_frequency(self, citations_df: pd.DataFrame) -> pd.DataFrame:
        """Анализ частоты журналов"""
        total_cits = len(citations_df)
        unique_df = self.get_unique_citations(citations_df)
        total_unique = len(unique_df)

        journals_total = citations_df['journal_abbreviation']
        journals_total = journals_total[journals_total.isin(['Unknown', 'Error']) == False]
        journal_freq_total = journals_total.value_counts().reset_index()
        journal_freq_total.columns = ['journal', 'frequency_total']
        journal_freq_total['percentage_total'] = (journal_freq_total['frequency_total'] / total_cits * 100).round(2)

        journals_unique = unique_df['journal_abbreviation']
        journals_unique = journals_unique[journals_unique.isin(['Unknown', 'Error']) == False]
        journal_freq_unique = journals_unique.value_counts().reset_index()
        journal_freq_unique.columns = ['journal', 'frequency_unique']
        journal_freq_unique['percentage_unique'] = (journal_freq_unique['frequency_unique'] / total_unique * 100).round(2)

        journal_freq = journal_freq_total.merge(journal_freq_unique, on='journal', how='outer').fillna(0)
        return journal_freq[['journal', 'frequency_total', 'percentage_total', 'frequency_unique', 'percentage_unique']].sort_values('frequency_total', ascending=False)

    def analyze_affiliations_frequency(self, citations_df: pd.DataFrame) -> pd.DataFrame:
        """Анализ частоты аффилиаций"""
        total_cits = len(citations_df)
        unique_df = self.get_unique_citations(citations_df)
        total_unique = len(unique_df)

        affiliations_total = citations_df['affiliations'].str.split(';', expand=True).stack()
        affiliations_total = affiliations_total[affiliations_total.str.strip().isin(['Unknown', 'Error']) == False]
        affil_freq_total = affiliations_total.value_counts().reset_index()
        affil_freq_total.columns = ['affiliation', 'frequency_total']
        affil_freq_total['percentage_total'] = (affil_freq_total['frequency_total'] / total_cits * 100).round(2)

        affiliations_unique = unique_df['affiliations'].str.split(';', expand=True).stack()
        affiliations_unique = affiliations_unique[affiliations_unique.str.strip().isin(['Unknown', 'Error']) == False]
        affil_freq_unique = affiliations_unique.value_counts().reset_index()
        affil_freq_unique.columns = ['affiliation', 'frequency_unique']
        affil_freq_unique['percentage_unique'] = (affil_freq_unique['frequency_unique'] / total_unique * 100).round(2)

        affil_freq = affil_freq_total.merge(affil_freq_unique, on='affiliation', how='outer').fillna(0)
        return affil_freq[['affiliation', 'frequency_total', 'percentage_total', 'frequency_unique', 'percentage_unique']].sort_values('frequency_total', ascending=False)

    def analyze_countries_frequency(self, citations_df: pd.DataFrame) -> pd.DataFrame:
        """Анализ частоты стран и коллабораций"""
        total_cits = len(citations_df)
        unique_df = self.get_unique_citations(citations_df)
        total_unique = len(unique_df)

        country_counter_total = Counter()
        for countries in citations_df['countries']:
            if countries not in ['Unknown', 'Error']:
                country_counter_total[countries] += 1

        country_freq_total = pd.DataFrame({
            'countries': list(country_counter_total.keys()),
            'frequency_total': list(country_counter_total.values())
        })
        country_freq_total['percentage_total'] = (country_freq_total['frequency_total'] / total_cits * 100).round(2)

        country_counter_unique = Counter()
        for countries in unique_df['countries']:
            if countries not in ['Unknown', 'Error']:
                country_counter_unique[countries] += 1

        country_freq_unique = pd.DataFrame({
            'countries': list(country_counter_unique.keys()),
            'frequency_unique': list(country_counter_unique.values())
        })
        country_freq_unique['percentage_unique'] = (country_freq_unique['frequency_unique'] / total_unique * 100).round(2)

        country_freq = country_freq_total.merge(country_freq_unique, on='countries', how='outer').fillna(0)
        return country_freq[['countries', 'frequency_total', 'percentage_total', 'frequency_unique', 'percentage_unique']].sort_values('frequency_total', ascending=False)

    def analyze_year_distribution(self, citations_df: pd.DataFrame) -> pd.DataFrame:
        """Анализ распределения по годам"""
        total_cits = len(citations_df)
        unique_df = self.get_unique_citations(citations_df)
        total_unique = len(unique_df)

        years_total = pd.to_numeric(citations_df['year'], errors='coerce')
        years_total = years_total[years_total.notna() & years_total.between(1900, 2026)].astype(int)
        year_counts_total = years_total.value_counts().reset_index()
        year_counts_total.columns = ['year', 'frequency_total']
        year_counts_total['percentage_total'] = (year_counts_total['frequency_total'] / total_cits * 100).round(2)

        years_unique = pd.to_numeric(unique_df['year'], errors='coerce')
        years_unique = years_unique[years_unique.notna() & years_unique.between(1900, 2026)].astype(int)
        year_counts_unique = years_unique.value_counts().reset_index()
        year_counts_unique.columns = ['year', 'frequency_unique']
        year_counts_unique['percentage_unique'] = (year_counts_unique['frequency_unique'] / total_unique * 100).round(2)

        year_counts = year_counts_total.merge(year_counts_unique, on='year', how='outer').fillna(0)
        return year_counts[['year', 'frequency_total', 'percentage_total', 'frequency_unique', 'percentage_unique']].sort_values('year')

    def analyze_five_year_periods(self, citations_df: pd.DataFrame) -> pd.DataFrame:
        """Анализ пятилетних периодов с 1900 года, включая период 2025-2029"""
        total_cits = len(citations_df)
        unique_df = self.get_unique_citations(citations_df)
        total_unique = len(unique_df)

        start_year = 1900
        current_year = 2029
        bins = list(range(start_year, current_year + 1, 5))
        if bins[-1] < current_year:
            bins.append(current_year + 1)
        labels = [f"{start}-{start+4}" for start in bins[:-1]]

        years_total = pd.to_numeric(citations_df['year'], errors='coerce')
        years_total = years_total[years_total.notna() & years_total.between(1900, current_year)].astype(int)
        period_counts_total = pd.cut(years_total, bins=bins, labels=labels, include_lowest=True, right=True).astype(str)
        period_df_total = period_counts_total.value_counts().reset_index()
        period_df_total.columns = ['period', 'frequency_total']
        period_df_total['percentage_total'] = (period_df_total['frequency_total'] / total_cits * 100).round(2)
        period_df_total['period'] = period_df_total['period'].astype(str)

        years_unique = pd.to_numeric(unique_df['year'], errors='coerce')
        years_unique = years_unique[years_unique.notna() & years_unique.between(1900, current_year)].astype(int)
        period_counts_unique = pd.cut(years_unique, bins=bins, labels=labels, include_lowest=True, right=True).astype(str)
        period_df_unique = period_counts_unique.value_counts().reset_index()
        period_df_unique.columns = ['period', 'frequency_unique']
        period_df_unique['percentage_unique'] = (period_df_unique['frequency_unique'] / total_unique * 100).round(2)
        period_df_unique['period'] = period_df_unique['period'].astype(str)

        period_df = period_df_total.merge(period_df_unique, on='period', how='outer').fillna(0)
        return period_df[['period', 'frequency_total', 'percentage_total', 'frequency_unique', 'percentage_unique']].sort_values('period')

    def find_duplicate_citations(self, citations_df: pd.DataFrame) -> pd.DataFrame:
        """Поиск дубликатов (цитирующие статьи, встречающиеся в более чем одной статье) с частотой"""
        citations_df['cit_id'] = citations_df['doi'].fillna('') + '|' + citations_df['title'].fillna('')
        cit_counts = citations_df.groupby('cit_id')['source_doi'].nunique().reset_index()
        duplicate_cit_ids = cit_counts[cit_counts['source_doi'] > 1]['cit_id']

        if duplicate_cit_ids.empty:
            columns = list(citations_df.columns) + ['frequency']
            columns.remove('cit_id')
            return pd.DataFrame(columns=columns)

        frequency_map = citations_df['cit_id'].value_counts().to_dict()
        duplicates = citations_df[citations_df['cit_id'].isin(duplicate_cit_ids)].copy()
        duplicates = duplicates.drop_duplicates(subset=['cit_id'], keep='first')
        duplicates = duplicates[~((duplicates['doi'].isna()) & (duplicates['title'] == 'Unknown'))]
        duplicates['frequency'] = duplicates['cit_id'].map(frequency_map)
        duplicates = duplicates.drop(columns=['cit_id'])
        return duplicates.sort_values(['frequency', 'doi'], ascending=[False, True])

    def save_all_data_to_csv(self, combined_df: pd.DataFrame, source_articles_df: pd.DataFrame, doi_list: List[str], total_citations: int, unique_dois: int) -> str:
        """Сохранение данных в ZIP-архив и предложение для скачивания"""
        timestamp = int(time.time())
        temp_dir = tempfile.mkdtemp()

        unique_df = self.get_unique_citations(combined_df)
        duplicate_df = self.find_duplicate_citations(combined_df)

        failed_df = combined_df[combined_df['error'].notna()][['source_doi', 'position', 'doi', 'error']].copy()
        failed_df.columns = ['source_doi', 'ref_number', 'reference_doi', 'error_description']

        stats = self.performance_monitor.get_stats()
        preliminary_info = f"""Preliminary Analysis Results
==========================
Total citations found: {total_citations}
Unique DOIs: {unique_dois}
Total citations processed: {len(combined_df)}
Unique citations: {len(unique_df)}
Successful citations: {len(combined_df[combined_df['error'].isna()])}
Failed citations: {len(failed_df)}
Total processing time: {stats.get('elapsed_seconds', 0):.2f} seconds ({stats.get('elapsed_minutes', 0):.2f} minutes)
Citations per article:
"""
        for doi in doi_list:
            preliminary_info += f"  {doi}: {len(combined_df[combined_df['source_doi'] == doi])} citations\n"

        summary_content = f"""{preliminary_info}
Detailed Analysis Summary
========================
Analysis performed on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
General Statistics
-----------------
- Total articles processed: {len(doi_list)}
- Total citations collected: {total_citations}
- Unique DOIs: {unique_dois}
- Total citations processed: {len(combined_df)}
- Unique citations: {len(unique_df)}
- Successful citations: {len(combined_df[combined_df['error'].isna()])}
- Failed citations: {len(combined_df[combined_df['error'].notna()])}
- Unique authors (with initials): {len(self.analyze_authors_frequency(combined_df))}
- Unique journals: {len(self.analyze_journals_frequency(combined_df))}
- Unique affiliations: {len(self.analyze_affiliations_frequency(combined_df))}
- Unique country combinations: {len(self.analyze_countries_frequency(combined_df))}
- Duplicate citations: {len(duplicate_df)}
Performance Statistics
-----------------
- Total requests: {stats.get('total_requests', 0)}
- Elapsed time (seconds): {stats.get('elapsed_seconds', 0):.2f}
- Elapsed time (minutes): {stats.get('elapsed_minutes', 0):.2f}
- Requests per second: {stats.get('requests_per_second', 0):.2f}
Generated Files and Their Descriptions
-------------------------------------
1. all_citations.csv
   Description: Contains all citing articles extracted from the analyzed articles, including unique, duplicate, and failed citations (with errors or missing DOI).
   Columns:
     - source_doi: DOI of the article being cited.
     - position: Position of the citing article in the citation list.
     - doi: DOI of the citing article.
     - title: Title of the citing article.
     - authors: Full names of the authors.
     - authors_surnames: Author surnames.
     - authors_with_initials: Authors in surname-initial format (e.g., Smith J.).
     - year: Publication year (from published-print or published-online).
     - journal_full_name: Full name of the journal.
     - journal_abbreviation: Abbreviated journal name.
     - publisher: Publisher of the journal.
     - citation_count_crossref: Citation count from Crossref.
     - citation_count_openalex: Citation count from OpenAlex.
     - annual_citation_rate_crossref: Annual citation rate (Crossref).
     - annual_citation_rate_openalex: Annual citation rate (OpenAlex).
     - years_since_publication: Years since publication.
     - affiliations: Affiliations of the authors (semicolon-separated).
     - countries: Country codes (e.g., US;CN).
     - error: Error message if the citation failed to process.
2. unique_citations.csv
   Description: Contains unique citing articles (based on DOI or title if DOI is missing) extracted from all articles.
   Columns: Same as all_citations.csv.
3. source_articles.csv
   Description: Contains metadata for the source articles provided as input DOIs.
   Columns: Same as all_citations.csv, except 'position' is None since source articles are not citations.
4. duplicate_citations.csv
   Description: Contains unique citing articles that appear in more than one analyzed article, with each citation listed once. Citations with missing DOI and title 'Unknown' are excluded.
   Columns: Same as all_citations.csv, plus:
     - frequency: Number of times the citation appears across all analyzed articles.
5. failed_citations.csv
   Description: Contains information about citations that failed to process, including those with missing or invalid DOI where no match was found by title.
   Columns:
     - source_doi: DOI of the article being cited.
     - ref_number: Position of the citation in the citation list.
     - reference_doi: DOI of the citing article (if available).
     - error_description: Description of the error encountered during processing.
6. updated_failed_citations_<timestamp>.csv
   Description: Contains the results of attempting to find DOIs for citations in failed_citations.csv by searching their titles via Crossref.
   Columns:
     - source_doi: DOI of the article being cited.
     - ref_number: Position of the citation in the citation list.
     - reference_doi: Original DOI of the citing article (if available).
     - error_description: Original error description.
     - updated_doi: DOI found by title search (if successful).
     - updated_error: Result of the title search (e.g., "DOI found: <DOI>" or "No DOI found for title '<title>'").
7. author_surnames_with_initials_frequency.csv
   Description: Frequency analysis of authors (in surname-initial format) across all and unique citations.
   Columns:
     - author_with_initial: Author name (e.g., Smith J.).
     - frequency_total: Number of occurrences in all citations.
     - percentage_total: Percentage of occurrences in all citations.
     - frequency_unique: Number of occurrences in unique citations.
     - percentage_unique: Percentage of occurrences in unique citations.
8. journals_frequency.csv
   Description: Frequency analysis of journals across all and unique citations.
   Columns:
     - journal: Abbreviated journal name.
     - frequency_total: Number of occurrences in all citations.
     - percentage_total: Percentage of occurrences in all citations.
     - frequency_unique: Number of occurrences in unique citations.
     - percentage_unique: Percentage of occurrences in unique citations.
9. affiliations_frequency.csv
   Description: Frequency analysis of affiliations across all and unique citations.
   Columns:
     - affiliation: Name of the affiliation.
     - frequency_total: Number of occurrences in all citations.
     - percentage_total: Percentage of occurrences in all citations.
     - frequency_unique: Number of occurrences in unique citations.
     - percentage_unique: Percentage of occurrences in unique citations.
10. countries_frequency.csv
    Description: Frequency analysis of country combinations (e.g., US;CN) across all and unique citations.
    Columns:
      - countries: Country codes (semicolon-separated, in uppercase, e.g., US;CN).
      - frequency_total: Number of occurrences in all citations.
      - percentage_total: Percentage of occurrences in all citations.
      - frequency_unique: Number of occurrences in unique citations.
      - percentage_unique: Percentage of occurrences in unique citations.
11. year_distribution.csv
    Description: Distribution of publication years across all and unique citations.
    Columns:
      - year: Publication year (from published-print or published-online).
      - frequency_total: Number of occurrences in all citations.
      - percentage_total: Percentage of occurrences in all citations.
      - frequency_unique: Number of occurrences in unique citations.
      - percentage_unique: Percentage of occurrences in unique citations.
12. five_year_periods.csv
    Description: Distribution of citations by five-year periods (starting from 1900, including 2025-2029).
    Columns:
      - period: Five-year period (e.g., 1900-1904, 2025-2029).
      - frequency_total: Number of occurrences in all citations.
      - percentage_total: Percentage of occurrences in all citations.
      - frequency_unique: Number of occurrences in unique citations.
      - percentage_unique: Percentage of occurrences in unique citations.
13. analysis_summary.csv
    Description: Summary statistics in CSV format (same as the general statistics above).
    Columns:
      - total_articles: Number of processed articles.
      - total_citations: Total number of citations.
      - unique_dois: Number of unique DOIs.
      - total_citations_processed: Total number of citations processed.
      - unique_citations: Number of unique citations.
      - successful_citations: Number of successfully processed citations.
      - failed_citations: Number of citations with errors.
      - unique_authors_with_initials: Number of unique authors.
      - unique_journals: Number of unique journals.
      - unique_affiliations: Number of unique affiliations.
      - unique_countries: Number of unique country combinations.
      - duplicate_citations: Number of duplicate citations.
--------------------
This file serves as the starting point for understanding the analysis results. Each CSV file provides specific insights into the citations extracted from the input articles.
"""
        with open(os.path.join(temp_dir, "summary.txt"), 'w', encoding='utf-8') as f:
            f.write(summary_content)
        self.logger.info("Summary file created")

        combined_df.to_csv(os.path.join(temp_dir, "all_citations.csv"), index=False, encoding='utf-8')
        unique_df.to_csv(os.path.join(temp_dir, "unique_citations.csv"), index=False, encoding='utf-8')
        source_articles_df.to_csv(os.path.join(temp_dir, "source_articles.csv"), index=False, encoding='utf-8')
        if not duplicate_df.empty:
            duplicate_df.to_csv(os.path.join(temp_dir, "duplicate_citations.csv"), index=False, encoding='utf-8')
        if not failed_df.empty:
            failed_df.to_csv(os.path.join(temp_dir, "failed_citations.csv"), index=False, encoding='utf-8')
            failed_citations_path = os.path.join(temp_dir, "failed_citations.csv")
            updated_failed_path = self.process_failed_references(failed_citations_path, temp_dir)
            if updated_failed_path:
                self.logger.info(f"Updated failed citations included in ZIP: {updated_failed_path}")

        for df, name in [
            (self.analyze_authors_frequency(combined_df), 'author_surnames_with_initials_frequency'),
            (self.analyze_journals_frequency(combined_df), 'journals_frequency'),
            (self.analyze_affiliations_frequency(combined_df), 'affiliations_frequency'),
            (self.analyze_countries_frequency(combined_df), 'countries_frequency'),
            (self.analyze_year_distribution(combined_df), 'year_distribution'),
            (self.analyze_five_year_periods(combined_df), 'five_year_periods')
        ]:
            if not df.empty:
                df.to_csv(os.path.join(temp_dir, f"{name}.csv"), index=False, encoding='utf-8')
                self.logger.info(f"{name} created")

        summary_data = {
            'total_articles': len(doi_list),
            'total_citations': total_citations,
            'unique_dois': unique_dois,
            'total_citations_processed': len(combined_df),
            'unique_citations': len(unique_df),
            'successful_citations': len(combined_df[combined_df['error'].isna()]),
            'failed_citations': len(combined_df[combined_df['error'].notna()]),
            'unique_authors_with_initials': len(self.analyze_authors_frequency(combined_df)),
            'unique_journals': len(self.analyze_journals_frequency(combined_df)),
            'unique_affiliations': len(self.analyze_affiliations_frequency(combined_df)),
            'unique_countries': len(self.analyze_countries_frequency(combined_df)),
            'duplicate_citations': len(duplicate_df)
        }
        pd.DataFrame([summary_data]).to_csv(os.path.join(temp_dir, "analysis_summary.csv"), index=False, encoding='utf-8')
        self.logger.info("Analysis summary created")

        zip_path = os.path.join(tempfile.gettempdir(), f"analysis_results_{timestamp}.zip")
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files_list in os.walk(temp_dir):
                for file in files_list:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, temp_dir)
                    zipf.write(file_path, arcname)
        self.logger.info(f"ZIP archive created: analysis_results_{timestamp}.zip")
        files.download(zip_path)
        shutil.rmtree(temp_dir)
        self.logger.info("Temporary files removed")
        return f"analysis_results_{timestamp}.zip"

    def display_analysis_results(self, combined_df: pd.DataFrame, source_articles_df: pd.DataFrame, doi_list: List[str], total_citations: int, unique_dois: int):
        """Отображение результатов анализа"""
        print(f"\n{'='*80}\nCOMPREHENSIVE ANALYSIS RESULTS FOR {len(doi_list)} ARTICLES\n{'='*80}")
        if combined_df.empty and source_articles_df.empty:
            print("No data available")
            zip_name = self.save_all_data_to_csv(combined_df, source_articles_df, doi_list, total_citations, unique_dois)
            print(f"\nAll data archived and ready for download as: {zip_name}")
            return

        unique_df = self.get_unique_citations(combined_df)
        successful_cits = len(combined_df[combined_df['error'].isna()])
        stats = self.performance_monitor.get_stats()
        print(f"Total citations found: {total_citations}")
        print(f"Unique DOIs: {unique_dois}")
        print(f"Total citations processed: {len(combined_df)}")
        print(f"Unique citations: {len(unique_df)}")
        print(f"Successful citations: {successful_cits}")
        print(f"Failed citations: {len(combined_df[combined_df['error'].notna()])}")
        print(f"Total processing time: {stats.get('elapsed_seconds', 0):.2f} seconds ({stats.get('elapsed_minutes', 0):.2f} minutes)")
        print(f"\nCitations per article:")
        for doi in doi_list:
            print(f"  {doi}: {len(combined_df[combined_df['source_doi'] == doi])} citations")

        display_cols = ['source_doi', 'position', 'doi', 'title', 'authors_with_initials', 'year',
                        'journal_abbreviation', 'publisher', 'countries', 'citation_count_crossref', 'citation_count_openalex']
        pd.set_option('display.max_colwidth', 25)
        pd.set_option('display.max_rows', 50)

        try:
            print("\nSOURCE ARTICLES:")
            display(source_articles_df[display_cols].head(50))

            print("\nUNIQUE CITATIONS:")
            display(unique_df[display_cols].head(50))

            print(f"\n{'='*60}\nDUPLICATE CITATIONS\n{'='*60}")
            duplicate_df = self.find_duplicate_citations(combined_df)
            if not duplicate_df.empty:
                display(duplicate_df[display_cols + ['frequency']].head(15))

            print(f"\n{'='*60}\nCOUNTRIES FREQUENCY\n{'='*60}")
            country_freq_df = self.analyze_countries_frequency(combined_df)
            if not country_freq_df.empty:
                display(country_freq_df)

            print(f"\n{'='*60}\nYEAR DISTRIBUTION\n{'='*60}")
            display(self.analyze_year_distribution(combined_df))

            print(f"\n{'='*60}\nFIVE-YEAR PERIODS\n{'='*60}")
            five_year_df = self.analyze_five_year_periods(combined_df)
            if not five_year_df.empty:
                display(five_year_df)

            print(f"\n{'='*60}\nTOP 15 AUTHORS\n{'='*60}")
            display(self.analyze_authors_frequency(combined_df).head(15))

            print(f"\n{'='*60}\nTOP 15 JOURNALS\n{'='*60}")
            display(self.analyze_journals_frequency(combined_df).head(15))

            print(f"\n{'='*60}\nTOP 15 AFFILIATIONS\n{'='*60}")
            display(self.analyze_affiliations_frequency(combined_df).head(15))

        except Exception as e:
            self.logger.error(f"Error during display_analysis_results: {e}")
            print(f"Error displaying some analysis results: {e}")

        finally:
            zip_name = self.save_all_data_to_csv(combined_df, source_articles_df, doi_list, total_citations, unique_dois)
            print(f"\nAll data archived and ready for download as: {zip_name}")

    def parse_doi_input(self, input_text: str, max_dois: int = 200) -> List[str]:
        """Извлечение и нормализация DOI из текста, с учетом только уникальных DOI"""
        doi_pattern = r'(10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+)'
        dois = re.findall(doi_pattern, input_text, re.IGNORECASE)
        cleaned_dois = [self.normalize_doi(doi) for doi in dois if self.validate_doi(doi)]

        unique_dois = list(dict.fromkeys(cleaned_dois))
        if len(cleaned_dois) > len(unique_dois):
            self.logger.warning(f"Found {len(cleaned_dois) - len(unique_dois)} duplicate DOIs in input. Only {len(unique_dois)} unique DOIs will be processed.")
            print(f"Warning: Found {len(cleaned_dois) - len(unique_dois)} duplicate DOIs. Processing {len(unique_dois)} unique DOIs.")

        unique_dois = unique_dois[:max_dois]
        if len(cleaned_dois) > max_dois:
            self.logger.warning(f"Input contains {len(cleaned_dois)} DOIs, but only the first {max_dois} unique DOIs will be processed.")
            print(f"Warning: Input contains {len(cleaned_dois)} DOIs, but only the first {max_dois} unique DOIs will be processed.")

        if not unique_dois:
            self.logger.error("No valid DOIs found in the input.")
            print("Error: No valid DOIs found in the input.")
        return unique_dois

def main():
    """Основная функция с интерактивным интерфейсом"""
    analyzer = CitationAnalyzer()
    doi_input = widgets.Textarea(
        value='',
        placeholder='Enter DOIs (e.g., 10.1016/j.ceramint.2022.10.123, https://doi.org/10.1039/D2TA00001A, etc.) separated by any punctuation or newlines',
        description='DOIs:',
        layout={'width': '800px', 'height': '200px'}
    )
    submit_button = widgets.Button(description="Analyze DOIs")
    output = widgets.Output()

    display(doi_input, submit_button, output)

    def on_button_clicked(b):
        with output:
            output.clear_output()
            input_text = doi_input.value
            doi_list = analyzer.parse_doi_input(input_text)
            if not doi_list:
                print("No valid DOIs provided. Please enter at least one valid DOI.")
                return
            analyzer.logger.info(f"Analyzing {len(doi_list)} articles...")
            combined_citations_df, source_articles_df = analyzer.process_dois_parallel(doi_list)
            all_citations, unique_dois = analyzer.collect_all_citations(doi_list)
            analyzer.display_analysis_results(combined_citations_df, source_articles_df, doi_list, len(all_citations), len(unique_dois))

    submit_button.on_click(on_button_clicked)

if __name__ == "__main__":
    main()