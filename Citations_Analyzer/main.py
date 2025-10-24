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
from tqdm import tqdm
from contextlib import redirect_stdout
from io import StringIO
from ratelimit import limits, sleep_and_retry
from tenacity import retry, stop_after_attempt, wait_exponential
import logging
from bs4 import BeautifulSoup
import io
import csv
import nltk
from nltk.corpus import stopwords
from nltk.stem import PorterStemmer
import base64
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows

try:
    nltk.data.find('corpora/stopwords')
except LookupError:
    nltk.download('stopwords')


class Config:
    REQUEST_TIMEOUT = 30
    MAX_RETRIES = 3
    DELAY_BETWEEN_REQUESTS = 0.05


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
        self._unique_citations_cache = {}
        self.unique_ref_data_cache = {}
        self.unique_citation_data_cache = {}
        self.ltwa_map = None
        self.stop_words = set(stopwords.words('english'))
        self.stemmer = PorterStemmer()
        self.scientific_stopwords = {
            'using', 'based', 'study', 'studies', 'research', 'analysis',
            'effect', 'effects', 'properties', 'property', 'development',
            'application', 'applications', 'method', 'methods', 'approach',
            'review', 'investigation', 'characterization', 'evaluation',
            'performance', 'behavior', 'structure', 'synthesis', 'design',
            'fabrication', 'preparation', 'processing', 'measurement',
            'model', 'models', 'system', 'systems', 'technology', 'material',
            'materials', 'sample', 'samples', 'device', 'devices', 'film',
            'films', 'layer', 'layers', 'surface', 'surfaces', 'interface',
            'interfaces', 'nanoparticle', 'nanoparticles', 'nanostructure',
            'nanostructures', 'composite', 'composites', 'coating', 'coatings'
        }
        self.scientific_stopwords_stemmed = {self.stemmer.stem(word) for word in self.scientific_stopwords}
        self.setup_logging()

    def setup_logging(self):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(f'doi_analyzer_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)

    def validate_doi(self, doi: str) -> bool:
        """Проверяет валидность DOI с улучшенной обработкой"""
        if not doi or not isinstance(doi, str):
            return False

        # Нормализуем DOI перед проверкой
        doi = self.normalize_doi(doi)

        # Основной паттерн DOI
        doi_pattern = r'^10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+$'

        # Дополнительные проверки
        if not bool(re.match(doi_pattern, doi, re.IGNORECASE)):
            return False

        # Проверка минимальной длины
        if len(doi) < 10:
            return False

        # Проверка на наличие только разрешенных символов
        if re.search(r'[^\w\.\-_;()/:]', doi):
            return False

        return True

    def normalize_doi(self, doi: str) -> str:
        """Нормализует DOI, убирая префиксы и лишние символы"""
        if not doi or not isinstance(doi, str):
            return ""

        doi = doi.strip()

        # Убираем различные префиксы
        prefixes = [
            'https://doi.org/',
            'http://doi.org/',
            'doi.org/',
            'doi:',
            'DOI:',
            'https://dx.doi.org/',
            'http://dx.doi.org/',
        ]

        for prefix in prefixes:
            if doi.lower().startswith(prefix.lower()):
                doi = doi[len(prefix):]
                break

        # Убираем параметры URL и якоря
        doi = doi.split('?')[0].split('#')[0]

        # Убираем лишние пробелы и символы
        doi = doi.strip()

        return doi.lower()

    def parse_doi_input(self, input_text: str, max_dois: int = 200) -> List[str]:
        """Парсит ввод DOI с улучшенной обработкой различных форматов"""
        if not input_text or not isinstance(input_text, str):
            print("Error: Input is empty or not a string")
            return []

        # Убираем лишние пробелы и разбиваем на строки
        lines = input_text.strip().split('\n')

        dois = []
        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Удаляем точки и запятые в конце строки
            line = line.rstrip('.,;')

            # Ищем DOI в строке с помощью регулярного выражения
            doi_pattern = r'10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+'
            found_dois = re.findall(doi_pattern, line, re.IGNORECASE)

            if found_dois:
                dois.extend(found_dois)
            else:
                # Если не нашли DOI паттерн, пытаемся извлечь из URL или других форматов
                # Обработка URL форматов
                if 'doi.org/' in line.lower():
                    doi_part = line.lower().split('doi.org/')[-1]
                    doi_part = doi_part.split('?')[0].split('#')[0].strip()
                    if self.validate_doi(doi_part):
                        dois.append(doi_part)
                # Обработка формата "doi:10.xxx/xxx"
                elif line.lower().startswith('doi:'):
                    doi_part = line[4:].strip()
                    if self.validate_doi(doi_part):
                        dois.append(doi_part)
                # Если строка выглядит как чистый DOI
                elif self.validate_doi(line):
                    dois.append(line)

        # Нормализуем DOI и убираем дубликаты
        cleaned_dois = []
        for doi in dois:
            normalized_doi = self.normalize_doi(doi)
            if self.validate_doi(normalized_doi):
                cleaned_dois.append(normalized_doi)

        # Убираем дубликаты сохраняя порядок
        unique_dois = []
        seen = set()
        for doi in cleaned_dois:
            if doi not in seen:
                seen.add(doi)
                unique_dois.append(doi)

        unique_dois = unique_dois[:max_dois]

        # Вывод информации о найденных DOI
        if not unique_dois:
            print("Error: No valid DOIs found in the input.")
            print("Please check the format. Valid examples:")
            print("  - 10.1234/abcd.1234")
            print("  - https://doi.org/10.1234/abcd.1234")
            print("  - doi:10.1234/abcd.1234")
        else:
            print(f"Found {len(unique_dois)} valid DOI(s)")
            if len(cleaned_dois) > len(unique_dois):
                print(f"Removed {len(cleaned_dois) - len(unique_dois)} duplicate DOI(s)")
            if len(cleaned_dois) > max_dois:
                print(f"Limited to first {max_dois} unique DOI(s) from {len(cleaned_dois)} found")

        return unique_dois

    @sleep_and_retry
    @limits(calls=15, period=1)
    def get_openalex_data(self, doi: str) -> Dict:
        if doi in self.openalex_cache:
            return self.openalex_cache[doi]
        try:
            openalex_url = f"https://api.openalex.org/works/https://doi.org/{doi}"
            response = requests.get(openalex_url, timeout=Config.REQUEST_TIMEOUT)
            self.performance_monitor.increment_request()
            if response.status_code == 404:
                self.openalex_cache[doi] = {}
                return {}
            response.raise_for_status()
            result = response.json()
            self.openalex_cache[doi] = result
            return result
        except:
            self.openalex_cache[doi] = {}
            return {}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=5))
    @sleep_and_retry
    @limits(calls=15, period=1)
    def get_crossref_data(self, doi: str) -> Dict:
        if doi in self.crossref_cache:
            return self.crossref_cache[doi]
        try:
            cr = Crossref()
            result = cr.works(ids=doi)
            self.performance_monitor.increment_request()
            data = result['message']
            year = None
            for key in ['published-print', 'published-online', 'issued']:
                if key in data and 'date-parts' in data[key]:
                    date_parts = data[key]['date-parts'][0]
                    year = date_parts[0] if date_parts else None
                    break
            data['publication_year'] = year if year else 'Unknown'
            self.crossref_cache[doi] = data
            return data
        except:
            self.crossref_cache[doi] = {'publication_year': 'Unknown'}
            return {'publication_year': 'Unknown'}

    @sleep_and_retry
    @limits(calls=10, period=1)
    def quick_doi_search(self, title: str) -> str:
        """Quick DOI search by title"""
        if not title or title == 'Unknown':
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
                    return doi
            return None
        except:
            return None

    def safe_calculate_annual_citation_rate(self, citation_count, publication_year, current_year=None):
        """Безопасный расчет ежегодной цитируемости с обработкой ошибок"""
        try:
            if not isinstance(citation_count, (int, float)) or citation_count == 0:
                return 0.0

            years = self.calculate_years_since_publication(publication_year, current_year)

            # Защита от None и некорректных значений
            if years is None or not isinstance(years, (int, float)) or years <= 0:
                return 0.0

            return round(citation_count / years, 2)
        except (TypeError, ZeroDivisionError, ValueError):
            return 0.0

    def calculate_years_since_publication(self, publication_year: Any, current_year: int = None) -> int:
        """Безопасный расчет лет с момента публикации"""
        try:
            if current_year is None:
                current_year = datetime.now().year

            # Обработка различных форматов года
            if publication_year is None or publication_year == 'Unknown':
                return 1

            year_str = str(publication_year).strip()
            if not year_str or year_str == 'Unknown':
                return 1

            # Извлечение года из строки
            year_match = re.search(r'\b(19|20)\d{2}\b', year_str)
            if year_match:
                year = int(year_match.group())
            else:
                year = int(year_str)

            if 1900 < year <= current_year:
                return max(1, current_year - year)
            else:
                return 1
        except (ValueError, TypeError):
            return 1

    def get_combined_article_data(self, doi: str) -> Dict[str, Any]:
        """Get combined data from both Crossref and OpenAlex with fallback logic"""
        try:
            crossref_data = self.get_crossref_data(doi)
            openalex_data = self.get_openalex_data(doi)

            # Get title with fallback
            title = 'Unknown'
            if openalex_data and openalex_data.get('title'):
                title = openalex_data['title']
            elif crossref_data.get('title'):
                title_list = crossref_data['title']
                if title_list:
                    title = title_list[0]

            # Get year with fallback
            year = 'Unknown'
            publication_year = None

            if openalex_data and openalex_data.get('publication_year'):
                publication_year = openalex_data['publication_year']
                year = str(publication_year)
            elif crossref_data.get('publication_year') != 'Unknown':
                publication_year = crossref_data['publication_year']
                year = str(publication_year)

            # Get authors with combined data
            authors = []
            authors_surnames = []
            authors_with_initials = []

            # First try OpenAlex for authors
            if openalex_data:
                for author in openalex_data.get('authorships', []):
                    name = author.get('author', {}).get('display_name', 'Unknown')
                    if name != 'Unknown':
                        authors.append(name)
                        surname_with_initial = self.extract_surname_with_initial(name)
                        authors_surnames.append(surname_with_initial)
                        authors_with_initials.append(surname_with_initial)

            # If no authors from OpenAlex, try Crossref
            if not authors and crossref_data.get('author'):
                for author in crossref_data['author']:
                    given = author.get('given', '')
                    family = author.get('family', '')
                    if given or family:
                        name = f"{given} {family}".strip()
                        authors.append(name)
                        surname_with_initial = self.extract_surname_with_initial(name)
                        authors_surnames.append(surname_with_initial)
                        authors_with_initials.append(surname_with_initial)

            authors_str = ', '.join(authors) if authors else 'Unknown'
            authors_surnames_str = ', '.join(authors_surnames) if authors_surnames else 'Unknown'
            authors_with_initials_str = ', '.join(authors_with_initials) if authors_with_initials else 'Unknown'

            # Get journal info
            journal_info = self.get_journal_info_from_crossref(doi)

            # Get citation counts
            _, crossref_citations, openalex_citations = self.get_citation_data(doi)

            # Get affiliations and countries (primarily from OpenAlex)
            affiliations, countries = self.get_affiliations_and_countries_from_openalex(doi)

            # Calculate additional metrics
            current_year = datetime.now().year
            years_since_pub = self.calculate_years_since_publication(publication_year, current_year)

            return {
                'doi': doi,
                'title': title,
                'year': year,
                'publication_year': publication_year,
                'authors': authors_str,
                'authors_surnames': authors_surnames_str,
                'authors_with_initials': authors_with_initials_str,
                'journal_full_name': journal_info['full_name'],
                'journal_abbreviation': journal_info['abbreviation'],
                'publisher': journal_info['publisher'],
                'citation_count_crossref': crossref_citations,
                'citation_count_openalex': openalex_citations,
                'affiliations': '; '.join(affiliations),
                'countries': countries,
                'years_since_publication': years_since_pub
            }
        except Exception as e:
            # Возвращаем базовую структуру с информацией об ошибке
            return {
                'doi': doi,
                'title': 'Error',
                'year': 'Unknown',
                'publication_year': None,
                'authors': 'Error',
                'authors_surnames': 'Error',
                'authors_with_initials': 'Error',
                'journal_full_name': 'Error',
                'journal_abbreviation': 'Error',
                'publisher': 'Error',
                'citation_count_crossref': 0,
                'citation_count_openalex': 0,
                'affiliations': 'Error',
                'countries': 'Error',
                'years_since_publication': 1,
                'error': str(e)
            }

    def get_citation_data(self, doi: str) -> tuple:
        try:
            crossref_data = self.get_crossref_data(doi)
            crossref_citations = crossref_data.get('is-referenced-by-count', 0)

            openalex_data = self.get_openalex_data(doi)
            openalex_citations = openalex_data.get('cited_by_count', 0)

            return doi, crossref_citations, openalex_citations
        except:
            return doi, 0, 0

    def get_journal_info_from_crossref(self, doi: str) -> Dict[str, Any]:
        try:
            data = self.get_crossref_data(doi)
            container_title = data.get('container-title', [])
            short_container_title = data.get('short-container-title', [])
            full_name = container_title[0] if container_title else (
                short_container_title[0] if short_container_title else 'Unknown')
            abbreviation = short_container_title[0] if short_container_title else (
                container_title[0] if container_title else 'Unknown')
            return {
                'full_name': full_name,
                'abbreviation': abbreviation,
                'publisher': data.get('publisher', 'Unknown'),
                'issn': data.get('ISSN', [None])[0]
            }
        except:
            return {
                'full_name': 'Unknown',
                'abbreviation': 'Unknown',
                'publisher': 'Unknown',
                'issn': None
            }

    def get_affiliations_and_countries_from_openalex(self, doi: str) -> tuple[List[str], str]:
        try:
            data = self.get_openalex_data(doi)
            affiliations = set()
            countries = set()

            for authorship in data.get('authorships', []):
                for institution in authorship.get('institutions', []):
                    if name := institution.get('display_name'):
                        affiliations.add(name)
                    if country_code := institution.get('country_code'):
                        countries.add(country_code.upper())

            return list(affiliations) or ['Unknown'], ';'.join(sorted(countries)) if countries else 'Unknown'
        except:
            return ['Unknown'], 'Unknown'

    @lru_cache(maxsize=1000)
    def extract_surname_with_initial(self, author_name: str) -> str:
        if not author_name or author_name in ['Unknown', 'Error']:
            return author_name
        clean_name = re.sub(r'[^\w\s\-\.]', ' ', author_name).strip()
        parts = clean_name.split()
        if not parts:
            return author_name
        surname = parts[-1]
        initial = parts[0][0].upper() if parts[0] else ''
        return f"{surname} {initial}." if initial else surname

    def get_references_from_crossref(self, doi: str) -> List[Dict[str, Any]]:
        try:
            article_data = get_publication_as_json(doi)
            return article_data.get('reference', [])
        except:
            return []

    # НОВЫЙ ФУНКЦИОНАЛ: ПОИСК ЦИТИРУЮЩИХ ДОКУМЕНТОВ
    def get_citing_articles_from_openalex(self, doi: str) -> List[str]:
        """Получает цитирующие работы через OpenAlex API"""
        citing_dois = []
        try:
            # Получаем ID работы по DOI
            work_id = doi.replace('/', '%2F')
            url = f"https://api.openalex.org/works/https://doi.org/{work_id}"

            response = requests.get(url, timeout=Config.REQUEST_TIMEOUT)
            self.performance_monitor.increment_request()

            if response.status_code == 200:
                data = response.json()
                cited_by_count = data.get('cited_by_count', 0)

                if cited_by_count > 0:
                    # Получаем список цитирующих работ
                    citing_url = f"https://api.openalex.org/works?filter=cites:{data['id']}&per-page=200"

                    while citing_url:
                        response = requests.get(citing_url, timeout=Config.REQUEST_TIMEOUT)
                        self.performance_monitor.increment_request()

                        if response.status_code == 200:
                            citing_data = response.json()

                            for work in citing_data.get('results', []):
                                if work.get('doi'):
                                    citing_dois.append(work['doi'])

                            # Проверяем наличие следующей страницы
                            citing_url = citing_data.get('meta', {}).get('next_cursor')
                            time.sleep(0.1)

        except Exception as e:
            print(f"Ошибка при работе с OpenAlex для {doi}: {e}")

        return citing_dois

    def get_citing_articles_from_crossref(self, doi: str) -> List[str]:
        """Получает цитирующие работы через Crossref API"""
        citing_dois = []
        try:
            url = f"https://api.crossref.org/works/{doi}"
            response = requests.get(url, timeout=Config.REQUEST_TIMEOUT)
            self.performance_monitor.increment_request()

            if response.status_code == 200:
                data = response.json()
                if 'message' in data and 'is-referenced-by' in data['message']:
                    references = data['message']['is-referenced-by']
                    for ref in references:
                        if isinstance(ref, dict) and 'DOI' in ref:
                            citing_dois.append(ref['DOI'])

        except Exception as e:
            print(f"Ошибка при работе с Crossref для {doi}: {e}")

        return citing_dois

    def find_citing_articles(self, doi_list: List[str]) -> Dict[str, Dict]:
        """Основная функция для поиска цитирующих статей"""
        results = {}

        for i, doi in enumerate(doi_list, 1):
            doi = doi.strip()
            if not doi:
                continue

            print(f"🔍 [{i}/{len(doi_list)}] Поиск цитирований для: {doi}")

            # Получаем цитирования из разных источников
            openalex_citations = self.get_citing_articles_from_openalex(doi)
            crossref_citations = self.get_citing_articles_from_crossref(doi)

            # Объединяем и убираем дубликаты
            all_citations = list(set(openalex_citations + crossref_citations))

            results[doi] = {
                'count': len(all_citations),
                'citing_dois': all_citations
            }

            print(f"✅ Найдено цитирований: {len(all_citations)}")
            time.sleep(0.5)

        return results

    def process_citing_articles_sequential(self, doi_list: List[str]) -> tuple[
        pd.DataFrame, pd.DataFrame, Dict[str, Dict], List[str]]:
        """Обрабатывает цитирующие статьи последовательно с полной статистикой"""
        self.performance_monitor.start()

        print("🔍 Step 1: Поиск цитирующих статей...")
        citing_results = self.find_citing_articles(doi_list)

        print("🔍 Step 2: Сбор данных о цитирующих статьях...")
        all_citing_articles_data = []
        citing_articles_details = []
        all_citing_titles = []

        # Собираем все уникальные DOI цитирующих статей
        all_citing_dois = set()
        for source_data in citing_results.values():
            all_citing_dois.update(source_data['citing_dois'])

        # Обрабатываем каждую цитирующую статью
        for citing_doi in tqdm(all_citing_dois, desc="Обработка цитирующих статей"):
            try:
                article_data = self.get_combined_article_data(citing_doi)
                citing_row = {
                    'source_doi': 'N/A',  # Для единообразия структуры
                    'position': 'N/A',  # Для единообразия структуры
                    'doi': citing_doi,
                    'title': article_data['title'],
                    'authors': article_data['authors'],
                    'authors_surnames': article_data['authors_surnames'],
                    'authors_with_initials': article_data['authors_with_initials'],
                    'year': article_data['year'],
                    'journal_full_name': article_data['journal_full_name'],
                    'journal_abbreviation': article_data['journal_abbreviation'],
                    'publisher': article_data['publisher'],
                    'citation_count_crossref': article_data['citation_count_crossref'],
                    'citation_count_openalex': article_data['citation_count_openalex'],
                    'annual_citation_rate_crossref': self.safe_calculate_annual_citation_rate(
                        article_data['citation_count_crossref'], article_data.get('publication_year')
                    ),
                    'annual_citation_rate_openalex': self.safe_calculate_annual_citation_rate(
                        article_data['citation_count_openalex'], article_data.get('publication_year')
                    ),
                    'years_since_publication': article_data['years_since_publication'],
                    'affiliations': article_data['affiliations'],
                    'countries': article_data['countries'],
                    'error': None
                }
                all_citing_articles_data.append(citing_row)
                all_citing_titles.append(article_data['title'])
                time.sleep(Config.DELAY_BETWEEN_REQUESTS)
            except Exception as e:
                all_citing_articles_data.append({
                    'source_doi': 'N/A', 'position': 'N/A', 'doi': citing_doi, 'title': 'Error',
                    'authors': 'Error', 'authors_surnames': 'Error', 'authors_with_initials': 'Error',
                    'year': 'Unknown', 'journal_full_name': 'Error', 'journal_abbreviation': 'Error',
                    'publisher': 'Error', 'citation_count_crossref': 'N/A', 'citation_count_openalex': 'N/A',
                    'annual_citation_rate_crossref': 'N/A', 'annual_citation_rate_openalex': 'N/A',
                    'years_since_publication': 'N/A', 'affiliations': 'Error', 'countries': 'Error', 'error': str(e)
                })
                all_citing_titles.append('Error')

        # Создаем детализированную таблицу связей
        for source_doi, data in citing_results.items():
            for citing_doi in data['citing_dois']:
                citing_info = next((item for item in all_citing_articles_data if item['doi'] == citing_doi), {})
                citing_articles_details.append({
                    'source_doi': source_doi,
                    'citing_doi': citing_doi,
                    'citing_title': citing_info.get('title', 'Unknown'),
                    'citing_authors': citing_info.get('authors_with_initials', 'Unknown'),
                    'citing_year': citing_info.get('year', 'Unknown'),
                    'citing_journal': citing_info.get('journal_abbreviation', 'Unknown'),
                    'citation_count': citing_info.get('citation_count_openalex', 0)
                })

        # Создаем DataFrame
        citing_articles_df = pd.DataFrame(all_citing_articles_data)
        citing_details_df = pd.DataFrame(citing_articles_details)

        return citing_articles_df, citing_details_df, citing_results, all_citing_titles

    # МЕТОДЫ АНАЛИЗА ДЛЯ ЦИТИРУЮЩИХ СТАТЕЙ (аналогичные методам для references)
    def get_unique_citations(self, citations_df: pd.DataFrame) -> pd.DataFrame:
        """Получает уникальные цитирующие статьи"""
        cache_key = id(citations_df)
        if cache_key not in self._unique_citations_cache:
            citations_df['citation_id'] = citations_df['doi'].fillna('') + '|' + citations_df['title'].fillna('')
            unique_df = citations_df.drop_duplicates(subset=['citation_id'], keep='first').drop(columns=['citation_id'])
            self._unique_citations_cache[cache_key] = unique_df
        return self._unique_citations_cache[cache_key]

    def find_duplicate_citations(self, citations_df: pd.DataFrame) -> pd.DataFrame:
        """Находит дублирующиеся цитирующие статьи"""
        try:
            citations_df['citation_id'] = citations_df['doi'].fillna('') + '|' + citations_df['title'].fillna('')
            citation_counts = citations_df.groupby('citation_id')['source_doi'].nunique().reset_index()
            duplicate_citation_ids = citation_counts[citation_counts['source_doi'] > 1]['citation_id']

            if duplicate_citation_ids.empty:
                columns = list(citations_df.columns) + ['frequency']
                columns.remove('citation_id')
                return pd.DataFrame(columns=columns)

            frequency_map = citations_df['citation_id'].value_counts().to_dict()
            duplicates = citations_df[citations_df['citation_id'].isin(duplicate_citation_ids)].copy()
            duplicates = duplicates.drop_duplicates(subset=['citation_id'], keep='first')
            duplicates = duplicates[~((duplicates['doi'].isna()) & (duplicates['title'] == 'Unknown'))]
            duplicates['frequency'] = duplicates['citation_id'].map(frequency_map)
            duplicates = duplicates.drop(columns=['citation_id'])
            return duplicates.sort_values(['frequency', 'doi'], ascending=[False, True])
        except Exception as e:
            print(f"Error finding duplicate citations: {e}")
            return pd.DataFrame()

    def analyze_citation_authors_frequency(self, citations_df: pd.DataFrame) -> pd.DataFrame:
        """Анализ частоты авторов в цитирующих статьях"""
        try:
            total_citations = len(citations_df)
            unique_df = self.get_unique_citations(citations_df)
            total_unique = len(unique_df)

            authors_total = citations_df['authors_with_initials'].str.split(',', expand=True).stack()
            authors_total = authors_total[authors_total.str.strip().isin(['Unknown', 'Error']) == False]
            author_freq_total = authors_total.value_counts().reset_index()
            author_freq_total.columns = ['author_with_initial', 'frequency_total']
            author_freq_total['percentage_total'] = (
                        author_freq_total['frequency_total'] / total_citations * 100).round(2)

            authors_unique = unique_df['authors_with_initials'].str.split(',', expand=True).stack()
            authors_unique = authors_unique[authors_unique.str.strip().isin(['Unknown', 'Error']) == False]
            author_freq_unique = authors_unique.value_counts().reset_index()
            author_freq_unique.columns = ['author_with_initial', 'frequency_unique']
            author_freq_unique['percentage_unique'] = (
                        author_freq_unique['frequency_unique'] / total_unique * 100).round(2)

            author_freq = author_freq_total.merge(author_freq_unique, on='author_with_initial', how='outer').fillna(0)
            return author_freq[['author_with_initial', 'frequency_total', 'percentage_total', 'frequency_unique',
                                'percentage_unique']].sort_values('frequency_total', ascending=False)
        except Exception as e:
            print(f"Error in citation author frequency analysis: {e}")
            return pd.DataFrame()

    def analyze_citation_journals_frequency(self, citations_df: pd.DataFrame) -> pd.DataFrame:
        """Анализ частоты журналов в цитирующих статьях"""
        try:
            if self.ltwa_map is None:
                try:
                    ltwa_url = "https://www.issn.org/wp-content/uploads/2024/02/ltwa_current.csv"
                    ltwa_resp = requests.get(ltwa_url, timeout=10)
                    ltwa_df = pd.read_csv(io.StringIO(ltwa_resp.text), on_bad_lines='skip')
                    self.ltwa_map = dict(zip(ltwa_df['title_abbreviation'], ltwa_df['full_title']))
                except:
                    self.ltwa_map = {}

            total_citations = len(citations_df)
            unique_df = self.get_unique_citations(citations_df)
            total_unique = len(unique_df)

            journals_total = citations_df['journal_abbreviation']
            journals_total = journals_total[journals_total.isin(['Unknown', 'Error']) == False]
            journal_freq_total = journals_total.value_counts().reset_index()
            journal_freq_total.columns = ['journal_abbreviation', 'frequency_total']
            journal_freq_total['percentage_total'] = (
                        journal_freq_total['frequency_total'] / total_citations * 100).round(2)

            journals_unique = unique_df['journal_abbreviation']
            journals_unique = journals_unique[journals_unique.isin(['Unknown', 'Error']) == False]
            journal_freq_unique = journals_unique.value_counts().reset_index()
            journal_freq_unique.columns = ['journal_abbreviation', 'frequency_unique']
            journal_freq_unique['percentage_unique'] = (
                        journal_freq_unique['frequency_unique'] / total_unique * 100).round(2)

            journal_freq = journal_freq_total.merge(journal_freq_unique, on='journal_abbreviation', how='outer').fillna(
                0)
            journal_freq['journal_full_name'] = journal_freq['journal_abbreviation'].map(self.ltwa_map).fillna(
                'Unknown')

            return journal_freq[
                ['journal_abbreviation', 'journal_full_name', 'frequency_total', 'percentage_total', 'frequency_unique',
                 'percentage_unique']].sort_values('frequency_total', ascending=False)
        except Exception as e:
            print(f"Error in citation journal frequency analysis: {e}")
            return pd.DataFrame()

    def analyze_citation_affiliations_frequency(self, citations_df: pd.DataFrame) -> pd.DataFrame:
        """Анализ частоты аффилиаций в цитирующих статьях"""
        try:
            total_citations = len(citations_df)
            unique_df = self.get_unique_citations(citations_df)
            total_unique = len(unique_df)

            affiliations_total = citations_df['affiliations'].str.split(';', expand=True).stack()
            affiliations_total = affiliations_total[affiliations_total.str.strip().isin(['Unknown', 'Error']) == False]
            affil_freq_total = affiliations_total.value_counts().reset_index()
            affil_freq_total.columns = ['affiliation', 'frequency_total']
            affil_freq_total['percentage_total'] = (affil_freq_total['frequency_total'] / total_citations * 100).round(
                2)

            affiliations_unique = unique_df['affiliations'].str.split(';', expand=True).stack()
            affiliations_unique = affiliations_unique[
                affiliations_unique.str.strip().isin(['Unknown', 'Error']) == False]
            affil_freq_unique = affiliations_unique.value_counts().reset_index()
            affil_freq_unique.columns = ['affiliation', 'frequency_unique']
            affil_freq_unique['percentage_unique'] = (affil_freq_unique['frequency_unique'] / total_unique * 100).round(
                2)

            affil_freq = affil_freq_total.merge(affil_freq_unique, on='affiliation', how='outer').fillna(0)
            return affil_freq[['affiliation', 'frequency_total', 'percentage_total', 'frequency_unique',
                               'percentage_unique']].sort_values('frequency_total', ascending=False)
        except Exception as e:
            print(f"Error in citation affiliation frequency analysis: {e}")
            return pd.DataFrame()

    def analyze_citation_countries_frequency(self, citations_df: pd.DataFrame) -> pd.DataFrame:
        """Анализ частоты стран в цитирующих статьях"""
        try:
            total_citations = len(citations_df)
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
            country_freq_total['percentage_total'] = (
                        country_freq_total['frequency_total'] / total_citations * 100).round(2)

            country_counter_unique = Counter()
            for countries in unique_df['countries']:
                if countries not in ['Unknown', 'Error']:
                    country_counter_unique[countries] += 1

            country_freq_unique = pd.DataFrame({
                'countries': list(country_counter_unique.keys()),
                'frequency_unique': list(country_counter_unique.values())
            })
            country_freq_unique['percentage_unique'] = (
                        country_freq_unique['frequency_unique'] / total_unique * 100).round(2)

            country_freq = country_freq_total.merge(country_freq_unique, on='countries', how='outer').fillna(0)
            return country_freq[['countries', 'frequency_total', 'percentage_total', 'frequency_unique',
                                 'percentage_unique']].sort_values('frequency_total', ascending=False)
        except Exception as e:
            print(f"Error in citation country frequency analysis: {e}")
            return pd.DataFrame()

    def analyze_citation_year_distribution(self, citations_df: pd.DataFrame) -> pd.DataFrame:
        """Анализ распределения по годам для цитирующих статей"""
        try:
            total_citations = len(citations_df)
            unique_df = self.get_unique_citations(citations_df)
            total_unique = len(unique_df)

            years_total = pd.to_numeric(citations_df['year'], errors='coerce')
            years_total = years_total[years_total.notna() & years_total.between(1900, 2026)].astype(int)
            year_counts_total = years_total.value_counts().reset_index()
            year_counts_total.columns = ['year', 'frequency_total']
            year_counts_total['percentage_total'] = (
                        year_counts_total['frequency_total'] / total_citations * 100).round(2)

            years_unique = pd.to_numeric(unique_df['year'], errors='coerce')
            years_unique = years_unique[years_unique.notna() & years_unique.between(1900, 2026)].astype(int)
            year_counts_unique = years_unique.value_counts().reset_index()
            year_counts_unique.columns = ['year', 'frequency_unique']
            year_counts_unique['percentage_unique'] = (
                        year_counts_unique['frequency_unique'] / total_unique * 100).round(2)

            year_counts = year_counts_total.merge(year_counts_unique, on='year', how='outer').fillna(0)
            return year_counts[
                ['year', 'frequency_total', 'percentage_total', 'frequency_unique', 'percentage_unique']].sort_values(
                'year')
        except Exception as e:
            print(f"Error in citation year distribution analysis: {e}")
            return pd.DataFrame()

    def analyze_citation_five_year_periods(self, citations_df: pd.DataFrame) -> pd.DataFrame:
        """Анализ пятилетних периодов для цитирующих статей"""
        try:
            total_citations = len(citations_df)
            unique_df = self.get_unique_citations(citations_df)
            total_unique = len(unique_df)

            start_year = 1900
            current_year = datetime.now().year + 4
            period_starts = list(range(start_year, current_year + 1, 5))
            bins = period_starts + [period_starts[-1] + 5]
            labels = [f"{s}-{s + 4}" for s in period_starts]

            years_total = pd.to_numeric(citations_df['year'], errors='coerce')
            years_total = years_total[years_total.notna() & years_total.between(1900, current_year)].astype(int)
            period_counts_total = pd.cut(years_total, bins=bins, labels=labels, right=False).astype(str)
            period_df_total = period_counts_total.value_counts().reset_index()
            period_df_total.columns = ['period', 'frequency_total']
            period_df_total['percentage_total'] = (period_df_total['frequency_total'] / total_citations * 100).round(2)
            period_df_total['period'] = period_df_total['period'].astype(str)

            years_unique = pd.to_numeric(unique_df['year'], errors='coerce')
            years_unique = years_unique[years_unique.notna() & years_unique.between(1900, current_year)].astype(int)
            period_counts_unique = pd.cut(years_unique, bins=bins, labels=labels, right=False).astype(str)
            period_df_unique = period_counts_unique.value_counts().reset_index()
            period_df_unique.columns = ['period', 'frequency_unique']
            period_df_unique['percentage_unique'] = (period_df_unique['frequency_unique'] / total_unique * 100).round(2)
            period_df_unique['period'] = period_df_unique['period'].astype(str)

            period_df = period_df_total.merge(period_df_unique, on='period', how='outer').fillna(0)
            return period_df[
                ['period', 'frequency_total', 'percentage_total', 'frequency_unique', 'percentage_unique']].sort_values(
                'period')
        except Exception as e:
            print(f"Error in citation five-year period analysis: {e}")
            return pd.DataFrame()

    def save_citation_analysis_to_excel(self, citing_articles_df: pd.DataFrame, citing_details_df: pd.DataFrame,
                                        doi_list: List[str], citing_results: Dict, all_citing_titles: List[str]) -> str:
        """Сохраняет полный анализ цитирующих статей в Excel"""
        try:
            timestamp = int(time.time())
            temp_dir = tempfile.mkdtemp()

            # Create Excel workbook
            excel_path = os.path.join(tempfile.gettempdir(), f"citation_analysis_results_{timestamp}.xlsx")
            wb = Workbook()

            # Remove default sheet
            wb.remove(wb.active)

            # Prepare all dataframes with error handling
            try:
                unique_citations_df = self.get_unique_citations(citing_articles_df)
            except Exception as e:
                print(f"Error creating unique citations: {e}")
                unique_citations_df = pd.DataFrame()

            try:
                duplicate_citations_df = self.find_duplicate_citations(citing_articles_df)
            except Exception as e:
                print(f"Error finding duplicate citations: {e}")
                duplicate_citations_df = pd.DataFrame()

            stats = self.performance_monitor.get_stats()

            try:
                content_freq, compound_freq, scientific_freq = self.analyze_titles(all_citing_titles)
            except Exception as e:
                print(f"Error analyzing citation titles: {e}")
                content_freq, compound_freq, scientific_freq = Counter(), Counter(), Counter()

            # Create sheets with error handling
            sheets_data = [
                ('Source_Articles_Citations', citing_details_df),  # Связи между исходными и цитирующими статьями
                ('All_Citations', citing_articles_df),
                ('All_Unique_Citations', unique_citations_df),
                ('Duplicate_Citations', duplicate_citations_df)
            ]

            # Add analysis sheets with error handling
            analysis_methods = [
                ('Author_Frequency_Citations', self.analyze_citation_authors_frequency),
                ('Journal_Frequency_Citations', self.analyze_citation_journals_frequency),
                ('Affiliation_Frequency_Citations', self.analyze_citation_affiliations_frequency),
                ('Country_Frequency_Citations', self.analyze_citation_countries_frequency),
                ('Year_Distribution_Citations', self.analyze_citation_year_distribution),
                ('5_Years_Period_Citations', self.analyze_citation_five_year_periods)
            ]

            for sheet_name, method in analysis_methods:
                try:
                    result_df = method(citing_articles_df)
                    sheets_data.append((sheet_name, result_df))
                except Exception as e:
                    print(f"Error in {sheet_name}: {e}")
                    sheets_data.append((sheet_name, pd.DataFrame()))

            # Add title word frequency
            try:
                title_word_data = []
                for i, (word, count) in enumerate(content_freq.most_common(50), 1):
                    title_word_data.append({'Category': 'Content_Words', 'Rank': i, 'Word': word, 'Frequency': count})
                for i, (word, count) in enumerate(compound_freq.most_common(50), 1):
                    title_word_data.append({'Category': 'Compound_Words', 'Rank': i, 'Word': word, 'Frequency': count})
                for i, (word, count) in enumerate(scientific_freq.most_common(50), 1):
                    title_word_data.append(
                        {'Category': 'Scientific_Stopwords', 'Rank': i, 'Word': word, 'Frequency': count})

                title_word_df = pd.DataFrame(title_word_data)
                sheets_data.append(('Title_Word_Frequency_Citations', title_word_df))
            except Exception as e:
                print(f"Error creating citation title word frequency: {e}")
                sheets_data.append(('Title_Word_Frequency_Citations', pd.DataFrame()))

            # Add summary data
            try:
                total_citations = sum(data['count'] for data in citing_results.values())
                total_citing_articles = len(citing_articles_df) if not citing_articles_df.empty else 0
                total_unique_citations = len(unique_citations_df) if not unique_citations_df.empty else 0

                summary_data = {
                    'total_source_articles': len(doi_list),
                    'total_citation_relationships': total_citations,
                    'total_citing_articles_processed': total_citing_articles,
                    'unique_citing_articles': total_unique_citations,
                    'successful_citations': len(
                        citing_articles_df[citing_articles_df['error'].isna()]) if not citing_articles_df.empty else 0,
                    'failed_citations': len(
                        citing_articles_df[citing_articles_df['error'].notna()]) if not citing_articles_df.empty else 0,
                    'unique_authors_citations': len(self.analyze_citation_authors_frequency(
                        citing_articles_df)) if not citing_articles_df.empty else 0,
                    'unique_journals_citations': len(self.analyze_citation_journals_frequency(
                        citing_articles_df)) if not citing_articles_df.empty else 0,
                    'unique_affiliations_citations': len(self.analyze_citation_affiliations_frequency(
                        citing_articles_df)) if not citing_articles_df.empty else 0,
                    'unique_countries_citations': len(self.analyze_citation_countries_frequency(
                        citing_articles_df)) if not citing_articles_df.empty else 0,
                    'duplicate_citations': len(duplicate_citations_df) if not duplicate_citations_df.empty else 0,
                    'total_processing_time_seconds': stats.get('elapsed_seconds', 0),
                    'total_processing_time_minutes': stats.get('elapsed_minutes', 0),
                    'total_requests': stats.get('total_requests', 0),
                    'requests_per_second': stats.get('requests_per_second', 0)
                }
                summary_df = pd.DataFrame([summary_data])
                sheets_data.append(('Analysis_Summary_Citations', summary_df))
            except Exception as e:
                print(f"Error creating citation summary: {e}")
                sheets_data.append(('Analysis_Summary_Citations', pd.DataFrame()))

            # Add detailed summary text
            citing_info = ""
            if citing_results:
                citing_info = f"\nCitations per source article:"
                for doi, data in citing_results.items():
                    citing_info += f"\n  - {doi}: {data['count']} citations"

            summary_content = f"""CITATION ANALYSIS REPORT (CITING ARTICLES)
Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

ANALYSIS OVERVIEW
=================
Total source articles: {len(doi_list)}
Total citation relationships: {total_citations}
Total citing articles processed: {total_citing_articles}
Unique citing articles: {total_unique_citations}
Successful citations: {summary_data.get('successful_citations', 0)}
Failed citations: {summary_data.get('failed_citations', 0)}
{citing_info}

PERFORMANCE STATISTICS
======================
Total processing time: {stats.get('elapsed_seconds', 0):.2f} seconds ({stats.get('elapsed_minutes', 0):.2f} minutes)
Total API requests: {stats.get('total_requests', 0)}
Requests per second: {stats.get('requests_per_second', 0):.2f}

DATA QUALITY NOTES
==================
- Analysis focuses on articles that cite the source articles
- Combined data from Crossref and OpenAlex improves completeness
- All standard statistical analyses performed (authors, journals, countries, etc.)
- Error handling ensures report generation even with partial data
"""

            # Create sheets in Excel
            for sheet_name, df in sheets_data:
                try:
                    if not df.empty:
                        ws = wb.create_sheet(sheet_name)
                        for r in dataframe_to_rows(df, index=False, header=True):
                            ws.append(r)
                    else:
                        ws = wb.create_sheet(sheet_name)
                        ws.append([f"No data available for {sheet_name}"])
                except Exception as e:
                    print(f"Error creating sheet {sheet_name}: {e}")

            # Add summary sheet
            try:
                ws_summary = wb.create_sheet('Report_Summary_Citations')
                for line in summary_content.split('\n'):
                    ws_summary.append([line])
            except Exception as e:
                print(f"Error creating citation summary sheet: {e}")

            # Save Excel file
            wb.save(excel_path)

            # Cleanup
            shutil.rmtree(temp_dir)

            return excel_path

        except Exception as e:
            print(f"Critical error in save_citation_analysis_to_excel: {e}")
            # Создаем минимальный отчет в случае критической ошибки
            try:
                timestamp = int(time.time())
                excel_path = os.path.join(tempfile.gettempdir(), f"minimal_citation_analysis_results_{timestamp}.xlsx")
                wb = Workbook()
                ws = wb.active
                ws.title = "Error_Report_Citations"
                ws.append(["ERROR REPORT - CITATION ANALYSIS"])
                ws.append([f"Critical error during citation analysis: {str(e)}"])
                ws.append([f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"])
                ws.append(["DOIs processed:", ', '.join(doi_list)])
                wb.save(excel_path)
                return excel_path
            except:
                return "error_creating_citation_report"

    # ОСТАЛЬНЫЕ МЕТОДЫ ДЛЯ REFERENCES ANALYSIS (остаются без изменений)
    def process_doi_sequential(self, doi_list: List[str]) -> tuple[pd.DataFrame, pd.DataFrame, int, int, List[str]]:
        """Process DOIs sequentially to avoid API overload"""
        self.performance_monitor.start()

        print("Step 1: Collecting references from source articles...")
        all_references = []
        for doi in tqdm(doi_list, desc="Collecting references"):
            try:
                references = self.get_references_from_crossref(doi)
                for i, ref in enumerate(references):
                    all_references.append({
                        'source_doi': doi,
                        'position': i + 1,
                        'ref': ref
                    })
                time.sleep(Config.DELAY_BETWEEN_REQUESTS)
            except Exception as e:
                print(f"Error collecting references for {doi}: {e}")

        print("Step 2: Identifying unique DOIs and searching missing ones...")
        unique_dois = set()
        titles_to_search = set()
        all_titles = []

        for ref_data in all_references:
            ref = ref_data['ref']
            ref_doi = ref.get('DOI')
            title = ref.get('article-title', 'Unknown')
            all_titles.append(title)

            if ref_doi and self.validate_doi(ref_doi):
                unique_dois.add(ref_doi)
            elif title != 'Unknown':
                titles_to_search.add(title)

        # Search for DOIs by title
        title_to_doi = {}
        for title in tqdm(list(titles_to_search), desc="Searching DOIs by title"):
            doi = self.quick_doi_search(title)
            if doi and self.validate_doi(doi):
                normalized_doi = self.normalize_doi(doi)
                title_to_doi[title] = normalized_doi
                unique_dois.add(normalized_doi)
            time.sleep(Config.DELAY_BETWEEN_REQUESTS)

        print("Step 3: Processing unique DOIs...")
        # Pre-process unique DOIs
        for doi in tqdm(list(unique_dois), desc="Processing unique DOIs"):
            if doi not in self.unique_ref_data_cache:
                try:
                    article_data = self.get_combined_article_data(doi)
                    # Store in cache with complete structure
                    self.unique_ref_data_cache[doi] = {
                        'doi': doi,
                        'title': article_data['title'],
                        'authors': article_data['authors'],
                        'authors_surnames': article_data['authors_surnames'],
                        'authors_with_initials': article_data['authors_with_initials'],
                        'year': article_data['year'],
                        'journal_full_name': article_data['journal_full_name'],
                        'journal_abbreviation': article_data['journal_abbreviation'],
                        'publisher': article_data['publisher'],
                        'citation_count_crossref': article_data['citation_count_crossref'],
                        'citation_count_openalex': article_data['citation_count_openalex'],
                        'affiliations': article_data['affiliations'],
                        'countries': article_data['countries'],
                        'publication_year': article_data.get('publication_year'),
                        'years_since_publication': article_data['years_since_publication']
                    }
                except Exception as e:
                    self.unique_ref_data_cache[doi] = {
                        'doi': doi, 'title': 'Unknown', 'authors': 'Error',
                        'authors_surnames': 'Error', 'authors_with_initials': 'Error',
                        'year': 'Unknown', 'journal_full_name': 'Error',
                        'journal_abbreviation': 'Error', 'publisher': 'Error',
                        'citation_count_crossref': 'N/A', 'citation_count_openalex': 'N/A',
                        'affiliations': 'Error', 'countries': 'Error', 'error': str(e)
                    }
                time.sleep(Config.DELAY_BETWEEN_REQUESTS)

        print("Step 4: Building reference dataset...")
        results = []
        source_articles = []

        for doi in tqdm(doi_list, desc="Processing source articles"):
            # Process source article
            try:
                source_data = self.get_combined_article_data(doi)
                source_row = {
                    'source_doi': doi,
                    'position': None,
                    'doi': doi,
                    'title': source_data['title'],
                    'authors': source_data['authors'],
                    'authors_surnames': source_data['authors_surnames'],
                    'authors_with_initials': source_data['authors_with_initials'],
                    'year': source_data['year'],
                    'journal_full_name': source_data['journal_full_name'],
                    'journal_abbreviation': source_data['journal_abbreviation'],
                    'publisher': source_data['publisher'],
                    'citation_count_crossref': source_data['citation_count_crossref'],
                    'citation_count_openalex': source_data['citation_count_openalex'],
                    'annual_citation_rate_crossref': self.safe_calculate_annual_citation_rate(
                        source_data['citation_count_crossref'], source_data.get('publication_year')
                    ),
                    'annual_citation_rate_openalex': self.safe_calculate_annual_citation_rate(
                        source_data['citation_count_openalex'], source_data.get('publication_year')
                    ),
                    'years_since_publication': source_data['years_since_publication'],
                    'affiliations': source_data['affiliations'],
                    'countries': source_data['countries'],
                    'error': None
                }
                source_articles.append(source_row)
            except Exception as e:
                source_articles.append({
                    'source_doi': doi, 'position': None, 'doi': doi, 'title': 'Unknown',
                    'authors': 'Error', 'authors_surnames': 'Error', 'authors_with_initials': 'Error',
                    'year': 'Unknown', 'journal_full_name': 'Error', 'journal_abbreviation': 'Error',
                    'publisher': 'Error', 'citation_count_crossref': 'N/A', 'citation_count_openalex': 'N/A',
                    'annual_citation_rate_crossref': 'N/A', 'annual_citation_rate_openalex': 'N/A',
                    'years_since_publication': 'N/A', 'affiliations': 'Error', 'countries': 'Error', 'error': str(e)
                })

            # Process references for this article
            article_refs = [ref for ref in all_references if ref['source_doi'] == doi]
            for ref_data in article_refs:
                ref = ref_data['ref']
                position = ref_data['position']
                ref_doi = ref.get('DOI')
                title = ref.get('article-title', 'Unknown')

                if ref_doi and self.validate_doi(ref_doi) and ref_doi in self.unique_ref_data_cache:
                    # Use cached data
                    ref_info = self.unique_ref_data_cache[ref_doi].copy()
                    ref_row = {
                        'source_doi': doi,
                        'position': position,
                        'doi': ref_doi,
                        'title': ref_info['title'],
                        'authors': ref_info['authors'],
                        'authors_surnames': ref_info['authors_surnames'],
                        'authors_with_initials': ref_info['authors_with_initials'],
                        'year': ref_info['year'],
                        'journal_full_name': ref_info['journal_full_name'],
                        'journal_abbreviation': ref_info['journal_abbreviation'],
                        'publisher': ref_info['publisher'],
                        'citation_count_crossref': ref_info['citation_count_crossref'],
                        'citation_count_openalex': ref_info['citation_count_openalex'],
                        'annual_citation_rate_crossref': self.safe_calculate_annual_citation_rate(
                            ref_info['citation_count_crossref'], ref_info.get('publication_year')
                        ),
                        'annual_citation_rate_openalex': self.safe_calculate_annual_citation_rate(
                            ref_info['citation_count_openalex'], ref_info.get('publication_year')
                        ),
                        'years_since_publication': ref_info['years_since_publication'],
                        'affiliations': ref_info['affiliations'],
                        'countries': ref_info['countries'],
                        'error': None
                    }
                    results.append(ref_row)
                else:
                    # Try to find DOI by title
                    found_doi = title_to_doi.get(title)
                    if found_doi and found_doi in self.unique_ref_data_cache:
                        ref_info = self.unique_ref_data_cache[found_doi].copy()
                        ref_row = {
                            'source_doi': doi,
                            'position': position,
                            'doi': found_doi,
                            'title': ref_info['title'],
                            'authors': ref_info['authors'],
                            'authors_surnames': ref_info['authors_surnames'],
                            'authors_with_initials': ref_info['authors_with_initials'],
                            'year': ref_info['year'],
                            'journal_full_name': ref_info['journal_full_name'],
                            'journal_abbreviation': ref_info['journal_abbreviation'],
                            'publisher': ref_info['publisher'],
                            'citation_count_crossref': ref_info['citation_count_crossref'],
                            'citation_count_openalex': ref_info['citation_count_openalex'],
                            'annual_citation_rate_crossref': self.safe_calculate_annual_citation_rate(
                                ref_info['citation_count_crossref'], ref_info.get('publication_year')
                            ),
                            'annual_citation_rate_openalex': self.safe_calculate_annual_citation_rate(
                                ref_info['citation_count_openalex'], ref_info.get('publication_year')
                            ),
                            'years_since_publication': ref_info['years_since_publication'],
                            'affiliations': ref_info['affiliations'],
                            'countries': ref_info['countries'],
                            'error': None
                        }
                        results.append(ref_row)
                    else:
                        # No valid data found
                        results.append({
                            'source_doi': doi, 'position': position, 'doi': ref_doi, 'title': title,
                            'authors': 'Unknown', 'authors_surnames': 'Unknown', 'authors_with_initials': 'Unknown',
                            'year': ref.get('year', 'Unknown'), 'journal_full_name': 'Unknown',
                            'journal_abbreviation': 'Unknown', 'publisher': 'Unknown',
                            'citation_count_crossref': 'N/A', 'citation_count_openalex': 'N/A',
                            'annual_citation_rate_crossref': 'N/A', 'annual_citation_rate_openalex': 'N/A',
                            'years_since_publication': 'N/A', 'affiliations': 'Unknown', 'countries': 'Unknown',
                            'error': f"Invalid or missing DOI: {ref_doi}, no match found for title '{title}'"
                        })

            time.sleep(Config.DELAY_BETWEEN_REQUESTS)

        # Создаем DataFrame даже если есть ошибки
        try:
            combined_references_df = pd.DataFrame(results)
        except Exception as e:
            print(f"Error creating combined DataFrame: {e}")
            combined_references_df = pd.DataFrame()

        try:
            source_articles_df = pd.DataFrame(source_articles)
        except Exception as e:
            print(f"Error creating source articles DataFrame: {e}")
            source_articles_df = pd.DataFrame()

        # Data enhancement phase
        print("Step 5: Enhancing incomplete data...")
        try:
            combined_references_df = self.enhance_incomplete_data(combined_references_df)
        except Exception as e:
            print(f"Error in data enhancement: {e}")

        try:
            source_articles_df = self.enhance_incomplete_data(source_articles_df)
        except Exception as e:
            print(f"Error enhancing source articles: {e}")

        return combined_references_df, source_articles_df, len(all_references), len(unique_dois), all_titles

    def enhance_incomplete_data(self, references_df: pd.DataFrame) -> pd.DataFrame:
        """Enhance references with incomplete data"""
        if references_df.empty:
            return references_df

        enhanced_rows = []
        incomplete_count = 0

        for index, row in tqdm(references_df.iterrows(), total=len(references_df), desc="Enhancing data"):
            doi = row['doi']

            # Check if data needs enhancement
            needs_enhancement = (
                    pd.isna(doi) or
                    row['title'] == 'Unknown' or
                    row['authors'] == 'Unknown' or
                    row['affiliations'] == 'Unknown' or
                    row['countries'] == 'Unknown' or
                    pd.notna(row.get('error'))
            )

            if needs_enhancement and doi and self.validate_doi(doi):
                incomplete_count += 1
                try:
                    enhanced_data = self.get_combined_article_data(doi)
                    # Create a new row with enhanced data, preserving original structure
                    enhanced_row = {
                        'source_doi': row['source_doi'],
                        'position': row['position'],
                        'doi': doi,
                        'title': enhanced_data['title'],
                        'authors': enhanced_data['authors'],
                        'authors_surnames': enhanced_data['authors_surnames'],
                        'authors_with_initials': enhanced_data['authors_with_initials'],
                        'year': enhanced_data['year'],
                        'journal_full_name': enhanced_data['journal_full_name'],
                        'journal_abbreviation': enhanced_data['journal_abbreviation'],
                        'publisher': enhanced_data['publisher'],
                        'citation_count_crossref': enhanced_data['citation_count_crossref'],
                        'citation_count_openalex': enhanced_data['citation_count_openalex'],
                        'annual_citation_rate_crossref': self.safe_calculate_annual_citation_rate(
                            enhanced_data['citation_count_crossref'], enhanced_data.get('publication_year')
                        ),
                        'annual_citation_rate_openalex': self.safe_calculate_annual_citation_rate(
                            enhanced_data['citation_count_openalex'], enhanced_data.get('publication_year')
                        ),
                        'years_since_publication': enhanced_data['years_since_publication'],
                        'affiliations': enhanced_data['affiliations'],
                        'countries': enhanced_data['countries'],
                        'error': None
                    }
                    enhanced_rows.append(enhanced_row)
                    time.sleep(Config.DELAY_BETWEEN_REQUESTS)
                    continue
                except Exception as e:
                    print(f"Error enhancing DOI {doi}: {e}")

            # If no enhancement or error, keep original row
            enhanced_rows.append(row.to_dict())

        if incomplete_count > 0:
            print(f"Enhanced data for {incomplete_count} references")

        return pd.DataFrame(enhanced_rows)

    def reprocess_failed_references(self, failed_references_df: pd.DataFrame) -> pd.DataFrame:
        """Reprocess failed references to find missing DOIs and data"""
        if failed_references_df.empty:
            return failed_references_df

        print("Reprocessing failed references...")
        updated_rows = []

        for index, row in tqdm(failed_references_df.iterrows(), total=len(failed_references_df),
                               desc="Reprocessing failed"):
            original_doi = row.get('reference_doi') if 'reference_doi' in row else row.get('doi')
            error_description = row.get('error_description', '') if 'error_description' in row else row.get('error', '')

            # Try to extract title from error description or row data
            title_match = re.search(r"title '([^']+)'|title ([^,]+)", str(error_description))
            title = None
            if title_match:
                title = next((g for g in title_match.groups() if g), None)

            if not title and 'title' in row:
                title = row['title']

            found_doi = None
            if title and title != 'Unknown':
                found_doi = self.quick_doi_search(title)
                time.sleep(Config.DELAY_BETWEEN_REQUESTS)

            # If we found a DOI, try to get complete data
            if found_doi and self.validate_doi(found_doi):
                try:
                    enhanced_data = self.get_combined_article_data(found_doi)
                    enhanced_data.update({
                        'source_doi': row.get('source_doi'),
                        'position': row.get('position'),
                        'error': None,
                        'annual_citation_rate_crossref': self.safe_calculate_annual_citation_rate(
                            enhanced_data['citation_count_crossref'], enhanced_data.get('publication_year')
                        ),
                        'annual_citation_rate_openalex': self.safe_calculate_annual_citation_rate(
                            enhanced_data['citation_count_openalex'], enhanced_data.get('publication_year')
                        )
                    })
                    updated_rows.append(enhanced_data)
                    continue
                except Exception as e:
                    print(f"Error reprocessing DOI {found_doi}: {e}")

            # If no DOI found or error, keep original with updated info
            updated_row = row.copy()
            if 'updated_doi' not in updated_row:
                updated_row['updated_doi'] = found_doi if found_doi else original_doi
            if 'updated_error' not in updated_row:
                updated_row[
                    'updated_error'] = f"DOI found: {found_doi}" if found_doi else f"No DOI found for title '{title}'" if title else "No title available"

            updated_rows.append(updated_row)

        return pd.DataFrame(updated_rows)

    def get_unique_references(self, references_df: pd.DataFrame) -> pd.DataFrame:
        cache_key = id(references_df)
        if cache_key not in self._unique_references_cache:
            references_df['ref_id'] = references_df['doi'].fillna('') + '|' + references_df['title'].fillna('')
            unique_df = references_df.drop_duplicates(subset=['ref_id'], keep='first').drop(columns=['ref_id'])
            self._unique_references_cache[cache_key] = unique_df
        return self._unique_references_cache[cache_key]

    def analyze_authors_frequency(self, references_df: pd.DataFrame) -> pd.DataFrame:
        try:
            total_refs = len(references_df)
            unique_df = self.get_unique_references(references_df)
            total_unique = len(unique_df)
            authors_total = references_df['authors_with_initials'].str.split(',', expand=True).stack()
            authors_total = authors_total[authors_total.str.strip().isin(['Unknown', 'Error']) == False]
            author_freq_total = authors_total.value_counts().reset_index()
            author_freq_total.columns = ['author_with_initial', 'frequency_total']
            author_freq_total['percentage_total'] = (author_freq_total['frequency_total'] / total_refs * 100).round(2)
            authors_unique = unique_df['authors_with_initials'].str.split(',', expand=True).stack()
            authors_unique = authors_unique[authors_unique.str.strip().isin(['Unknown', 'Error']) == False]
            author_freq_unique = authors_unique.value_counts().reset_index()
            author_freq_unique.columns = ['author_with_initial', 'frequency_unique']
            author_freq_unique['percentage_unique'] = (
                        author_freq_unique['frequency_unique'] / total_unique * 100).round(2)
            author_freq = author_freq_total.merge(author_freq_unique, on='author_with_initial', how='outer').fillna(0)
            return author_freq[['author_with_initial', 'frequency_total', 'percentage_total', 'frequency_unique',
                                'percentage_unique']].sort_values('frequency_total', ascending=False)
        except Exception as e:
            print(f"Error in author frequency analysis: {e}")
            return pd.DataFrame()

    def analyze_journals_frequency(self, references_df: pd.DataFrame) -> pd.DataFrame:
        try:
            if self.ltwa_map is None:
                try:
                    ltwa_url = "https://www.issn.org/wp-content/uploads/2024/02/ltwa_current.csv"
                    ltwa_resp = requests.get(ltwa_url, timeout=10)
                    ltwa_df = pd.read_csv(io.StringIO(ltwa_resp.text), on_bad_lines='skip')
                    self.ltwa_map = dict(zip(ltwa_df['title_abbreviation'], ltwa_df['full_title']))
                except:
                    self.ltwa_map = {}
            total_refs = len(references_df)
            unique_df = self.get_unique_references(references_df)
            total_unique = len(unique_df)
            journals_total = references_df['journal_abbreviation']
            journals_total = journals_total[journals_total.isin(['Unknown', 'Error']) == False]
            journal_freq_total = journals_total.value_counts().reset_index()
            journal_freq_total.columns = ['journal_abbreviation', 'frequency_total']
            journal_freq_total['percentage_total'] = (journal_freq_total['frequency_total'] / total_refs * 100).round(2)
            journals_unique = unique_df['journal_abbreviation']
            journals_unique = journals_unique[journals_unique.isin(['Unknown', 'Error']) == False]
            journal_freq_unique = journals_unique.value_counts().reset_index()
            journal_freq_unique.columns = ['journal_abbreviation', 'frequency_unique']
            journal_freq_unique['percentage_unique'] = (
                        journal_freq_unique['frequency_unique'] / total_unique * 100).round(2)
            journal_freq = journal_freq_total.merge(journal_freq_unique, on='journal_abbreviation', how='outer').fillna(
                0)
            journal_freq['journal_full_name'] = journal_freq['journal_abbreviation'].map(self.ltwa_map).fillna(
                'Unknown')
            return journal_freq[
                ['journal_abbreviation', 'journal_full_name', 'frequency_total', 'percentage_total', 'frequency_unique',
                 'percentage_unique']].sort_values('frequency_total', ascending=False)
        except Exception as e:
            print(f"Error in journal frequency analysis: {e}")
            return pd.DataFrame()

    def analyze_affiliations_frequency(self, references_df: pd.DataFrame) -> pd.DataFrame:
        try:
            total_refs = len(references_df)
            unique_df = self.get_unique_references(references_df)
            total_unique = len(unique_df)
            affiliations_total = references_df['affiliations'].str.split(';', expand=True).stack()
            affiliations_total = affiliations_total[affiliations_total.str.strip().isin(['Unknown', 'Error']) == False]
            affil_freq_total = affiliations_total.value_counts().reset_index()
            affil_freq_total.columns = ['affiliation', 'frequency_total']
            affil_freq_total['percentage_total'] = (affil_freq_total['frequency_total'] / total_refs * 100).round(2)
            affiliations_unique = unique_df['affiliations'].str.split(';', expand=True).stack()
            affiliations_unique = affiliations_unique[
                affiliations_unique.str.strip().isin(['Unknown', 'Error']) == False]
            affil_freq_unique = affiliations_unique.value_counts().reset_index()
            affil_freq_unique.columns = ['affiliation', 'frequency_unique']
            affil_freq_unique['percentage_unique'] = (affil_freq_unique['frequency_unique'] / total_unique * 100).round(
                2)
            affil_freq = affil_freq_total.merge(affil_freq_unique, on='affiliation', how='outer').fillna(0)
            return affil_freq[['affiliation', 'frequency_total', 'percentage_total', 'frequency_unique',
                               'percentage_unique']].sort_values('frequency_total', ascending=False)
        except Exception as e:
            print(f"Error in affiliation frequency analysis: {e}")
            return pd.DataFrame()

    def analyze_countries_frequency(self, references_df: pd.DataFrame) -> pd.DataFrame:
        try:
            total_refs = len(references_df)
            unique_df = self.get_unique_references(references_df)
            total_unique = len(unique_df)
            country_counter_total = Counter()
            for countries in references_df['countries']:
                if countries not in ['Unknown', 'Error']:
                    country_counter_total[countries] += 1
            country_freq_total = pd.DataFrame({
                'countries': list(country_counter_total.keys()),
                'frequency_total': list(country_counter_total.values())
            })
            country_freq_total['percentage_total'] = (country_freq_total['frequency_total'] / total_refs * 100).round(2)
            country_counter_unique = Counter()
            for countries in unique_df['countries']:
                if countries not in ['Unknown', 'Error']:
                    country_counter_unique[countries] += 1
            country_freq_unique = pd.DataFrame({
                'countries': list(country_counter_unique.keys()),
                'frequency_unique': list(country_counter_unique.values())
            })
            country_freq_unique['percentage_unique'] = (
                        country_freq_unique['frequency_unique'] / total_unique * 100).round(2)
            country_freq = country_freq_total.merge(country_freq_unique, on='countries', how='outer').fillna(0)
            return country_freq[['countries', 'frequency_total', 'percentage_total', 'frequency_unique',
                                 'percentage_unique']].sort_values('frequency_total', ascending=False)
        except Exception as e:
            print(f"Error in country frequency analysis: {e}")
            return pd.DataFrame()

    def analyze_year_distribution(self, references_df: pd.DataFrame) -> pd.DataFrame:
        try:
            total_refs = len(references_df)
            unique_df = self.get_unique_references(references_df)
            total_unique = len(unique_df)
            years_total = pd.to_numeric(references_df['year'], errors='coerce')
            years_total = years_total[years_total.notna() & years_total.between(1900, 2026)].astype(int)
            year_counts_total = years_total.value_counts().reset_index()
            year_counts_total.columns = ['year', 'frequency_total']
            year_counts_total['percentage_total'] = (year_counts_total['frequency_total'] / total_refs * 100).round(2)
            years_unique = pd.to_numeric(unique_df['year'], errors='coerce')
            years_unique = years_unique[years_unique.notna() & years_unique.between(1900, 2026)].astype(int)
            year_counts_unique = years_unique.value_counts().reset_index()
            year_counts_unique.columns = ['year', 'frequency_unique']
            year_counts_unique['percentage_unique'] = (
                        year_counts_unique['frequency_unique'] / total_unique * 100).round(2)
            year_counts = year_counts_total.merge(year_counts_unique, on='year', how='outer').fillna(0)
            return year_counts[
                ['year', 'frequency_total', 'percentage_total', 'frequency_unique', 'percentage_unique']].sort_values(
                'year')
        except Exception as e:
            print(f"Error in year distribution analysis: {e}")
            return pd.DataFrame()

    def analyze_five_year_periods(self, references_df: pd.DataFrame) -> pd.DataFrame:
        try:
            total_refs = len(references_df)
            unique_df = self.get_unique_references(references_df)
            total_unique = len(unique_df)
            start_year = 1900
            current_year = datetime.now().year + 4
            period_starts = list(range(start_year, current_year + 1, 5))
            bins = period_starts + [period_starts[-1] + 5]
            labels = [f"{s}-{s + 4}" for s in period_starts]
            years_total = pd.to_numeric(references_df['year'], errors='coerce')
            years_total = years_total[years_total.notna() & years_total.between(1900, current_year)].astype(int)
            period_counts_total = pd.cut(years_total, bins=bins, labels=labels, right=False).astype(str)
            period_df_total = period_counts_total.value_counts().reset_index()
            period_df_total.columns = ['period', 'frequency_total']
            period_df_total['percentage_total'] = (period_df_total['frequency_total'] / total_refs * 100).round(2)
            period_df_total['period'] = period_df_total['period'].astype(str)
            years_unique = pd.to_numeric(unique_df['year'], errors='coerce')
            years_unique = years_unique[years_unique.notna() & years_unique.between(1900, current_year)].astype(int)
            period_counts_unique = pd.cut(years_unique, bins=bins, labels=labels, right=False).astype(str)
            period_df_unique = period_counts_unique.value_counts().reset_index()
            period_df_unique.columns = ['period', 'frequency_unique']
            period_df_unique['percentage_unique'] = (period_df_unique['frequency_unique'] / total_unique * 100).round(2)
            period_df_unique['period'] = period_df_unique['period'].astype(str)
            period_df = period_df_total.merge(period_df_unique, on='period', how='outer').fillna(0)
            return period_df[
                ['period', 'frequency_total', 'percentage_total', 'frequency_unique', 'percentage_unique']].sort_values(
                'period')
        except Exception as e:
            print(f"Error in five-year period analysis: {e}")
            return pd.DataFrame()

    def find_duplicate_references(self, references_df: pd.DataFrame) -> pd.DataFrame:
        try:
            references_df['ref_id'] = references_df['doi'].fillna('') + '|' + references_df['title'].fillna('')
            ref_counts = references_df.groupby('ref_id')['source_doi'].nunique().reset_index()
            duplicate_ref_ids = ref_counts[ref_counts['source_doi'] > 1]['ref_id']
            if duplicate_ref_ids.empty:
                columns = list(references_df.columns) + ['frequency']
                columns.remove('ref_id')
                return pd.DataFrame(columns=columns)
            frequency_map = references_df['ref_id'].value_counts().to_dict()
            duplicates = references_df[references_df['ref_id'].isin(duplicate_ref_ids)].copy()
            duplicates = duplicates.drop_duplicates(subset=['ref_id'], keep='first')
            duplicates = duplicates[~((duplicates['doi'].isna()) & (duplicates['title'] == 'Unknown'))]
            duplicates['frequency'] = duplicates['ref_id'].map(frequency_map)
            duplicates = duplicates.drop(columns=['ref_id'])
            return duplicates.sort_values(['frequency', 'doi'], ascending=[False, True])
        except Exception as e:
            print(f"Error finding duplicate references: {e}")
            return pd.DataFrame()

    def preprocess_content_words(self, text: str) -> List[str]:
        if not text or text in ['Unknown', 'Error']:
            return []
        text = text.lower()
        text = re.sub(r'[^a-zA-Z\s-]', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        words = text.split()
        content_words = []
        for word in words:
            if '-' in word:
                continue
            if len(word) > 2 and word not in self.stop_words:
                stemmed_word = self.stemmer.stem(word)
                if stemmed_word not in self.scientific_stopwords_stemmed:
                    content_words.append(stemmed_word)
        return content_words

    def extract_compound_words(self, text: str) -> List[str]:
        if not text or text in ['Unknown', 'Error']:
            return []
        text = text.lower()
        compound_words = re.findall(r'\b[a-z]{2,}-[a-z]{2,}(?:-[a-z]{2,})*\b', text)
        return [word for word in compound_words if not any(part in self.stop_words for part in word.split('-'))]

    def extract_scientific_stopwords(self, text: str) -> List[str]:
        if not text or text in ['Unknown', 'Error']:
            return []
        text = text.lower()
        text = re.sub(r'[^a-zA-Z\s]', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        words = text.split()
        scientific_words = []
        for word in words:
            if len(word) > 2:
                stemmed_word = self.stemmer.stem(word)
                if stemmed_word in self.scientific_stopwords_stemmed:
                    for original_word in self.scientific_stopwords:
                        if self.stemmer.stem(original_word) == stemmed_word:
                            scientific_words.append(original_word)
                            break
        return scientific_words

    def analyze_titles(self, titles: List[str]) -> tuple[Counter, Counter, Counter]:
        content_words = []
        compound_words = []
        scientific_words = []
        valid_titles = [t for t in titles if t not in ['Unknown', 'Error']]
        for title in valid_titles:
            content_words.extend(self.preprocess_content_words(title))
            compound_words.extend(self.extract_compound_words(title))
            scientific_words.extend(self.extract_scientific_stopwords(title))
        return Counter(content_words), Counter(compound_words), Counter(scientific_words)

    def save_all_data_to_excel(self, combined_df: pd.DataFrame, source_articles_df: pd.DataFrame,
                               doi_list: List[str], total_references: int, unique_dois: int,
                               all_titles: List[str]) -> str:
        """Сохраняет анализ references в Excel"""
        try:
            timestamp = int(time.time())
            temp_dir = tempfile.mkdtemp()

            # Create Excel workbook
            excel_path = os.path.join(tempfile.gettempdir(), f"references_analysis_results_{timestamp}.xlsx")
            wb = Workbook()

            # Remove default sheet
            wb.remove(wb.active)

            # Prepare all dataframes with error handling
            try:
                unique_df = self.get_unique_references(combined_df)
            except Exception as e:
                print(f"Error creating unique references: {e}")
                unique_df = pd.DataFrame()

            try:
                duplicate_df = self.find_duplicate_references(combined_df)
            except Exception as e:
                print(f"Error finding duplicates: {e}")
                duplicate_df = pd.DataFrame()

            try:
                failed_df = combined_df[combined_df['error'].notna()][['source_doi', 'position', 'doi', 'error']].copy()
                failed_df.columns = ['source_doi', 'ref_number', 'reference_doi', 'error_description']
            except Exception as e:
                print(f"Error creating failed references: {e}")
                failed_df = pd.DataFrame()

            # Reprocess failed references
            try:
                if not failed_df.empty:
                    reprocessed_failed_df = self.reprocess_failed_references(failed_df)
                else:
                    reprocessed_failed_df = failed_df
            except Exception as e:
                print(f"Error reprocessing failed references: {e}")
                reprocessed_failed_df = failed_df

            stats = self.performance_monitor.get_stats()

            try:
                content_freq, compound_freq, scientific_freq = self.analyze_titles(all_titles)
            except Exception as e:
                print(f"Error analyzing titles: {e}")
                content_freq, compound_freq, scientific_freq = Counter(), Counter(), Counter()

            # Create sheets with error handling
            sheets_data = [
                ('Source_Articles', source_articles_df),
                ('All_References', combined_df),
                ('All_Unique_References', unique_df),
                ('Duplicate_References', duplicate_df),
                ('Failed_References', failed_df),
                ('Reprocessed_Failed_References', reprocessed_failed_df)
            ]

            # Add analysis sheets with error handling
            analysis_methods = [
                ('Author_Frequency', self.analyze_authors_frequency),
                ('Journal_Frequency', self.analyze_journals_frequency),
                ('Affiliation_Frequency', self.analyze_affiliations_frequency),
                ('Country_Frequency', self.analyze_countries_frequency),
                ('Year_Distribution', self.analyze_year_distribution),
                ('5_Years_Period', self.analyze_five_year_periods)
            ]

            for sheet_name, method in analysis_methods:
                try:
                    result_df = method(combined_df)
                    sheets_data.append((sheet_name, result_df))
                except Exception as e:
                    print(f"Error in {sheet_name}: {e}")
                    sheets_data.append((sheet_name, pd.DataFrame()))

            # Add title word frequency
            try:
                title_word_data = []
                for i, (word, count) in enumerate(content_freq.most_common(50), 1):
                    title_word_data.append({'Category': 'Content_Words', 'Rank': i, 'Word': word, 'Frequency': count})
                for i, (word, count) in enumerate(compound_freq.most_common(50), 1):
                    title_word_data.append({'Category': 'Compound_Words', 'Rank': i, 'Word': word, 'Frequency': count})
                for i, (word, count) in enumerate(scientific_freq.most_common(50), 1):
                    title_word_data.append(
                        {'Category': 'Scientific_Stopwords', 'Rank': i, 'Word': word, 'Frequency': count})

                title_word_df = pd.DataFrame(title_word_data)
                sheets_data.append(('Title_Word_Frequency', title_word_df))
            except Exception as e:
                print(f"Error creating title word frequency: {e}")
                sheets_data.append(('Title_Word_Frequency', pd.DataFrame()))

            # Add summary data
            try:
                summary_data = {
                    'total_source_articles': len(doi_list),
                    'total_references_collected': total_references,
                    'unique_dois_identified': unique_dois,
                    'total_references_processed': len(combined_df) if not combined_df.empty else 0,
                    'unique_references': len(unique_df) if not unique_df.empty else 0,
                    'successful_references': len(
                        combined_df[combined_df['error'].isna()]) if not combined_df.empty else 0,
                    'failed_references': len(combined_df[combined_df['error'].notna()]) if not combined_df.empty else 0,
                    'unique_authors_with_initials': len(
                        self.analyze_authors_frequency(combined_df)) if not combined_df.empty else 0,
                    'unique_journals': len(
                        self.analyze_journals_frequency(combined_df)) if not combined_df.empty else 0,
                    'unique_affiliations': len(
                        self.analyze_affiliations_frequency(combined_df)) if not combined_df.empty else 0,
                    'unique_countries': len(
                        self.analyze_countries_frequency(combined_df)) if not combined_df.empty else 0,
                    'duplicate_references': len(duplicate_df) if not duplicate_df.empty else 0,
                    'total_processing_time_seconds': stats.get('elapsed_seconds', 0),
                    'total_processing_time_minutes': stats.get('elapsed_minutes', 0),
                    'total_requests': stats.get('total_requests', 0),
                    'requests_per_second': stats.get('requests_per_second', 0)
                }
                summary_df = pd.DataFrame([summary_data])
                sheets_data.append(('Analysis_Summary', summary_df))
            except Exception as e:
                print(f"Error creating summary: {e}")
                sheets_data.append(('Analysis_Summary', pd.DataFrame()))

            # Add detailed summary text
            summary_content = f"""REFERENCES ANALYSIS REPORT
Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

ANALYSIS OVERVIEW
=================
Total source articles: {len(doi_list)}
Total references collected: {total_references}
Unique DOIs identified: {unique_dois}
Total references processed: {len(combined_df) if not combined_df.empty else 0}
Unique references: {len(unique_df) if not unique_df.empty else 0}
Successful references: {summary_data.get('successful_references', 0)}
Failed references: {summary_data.get('failed_references', 0)}

PERFORMANCE STATISTICS
======================
Total processing time: {stats.get('elapsed_seconds', 0):.2f} seconds ({stats.get('elapsed_minutes', 0):.2f} minutes)
Total API requests: {stats.get('total_requests', 0)}
Requests per second: {stats.get('requests_per_second', 0):.2f}

DATA QUALITY NOTES
==================
- Analysis focuses on references cited by the source articles
- Combined data from Crossref and OpenAlex improves completeness
- All standard statistical analyses performed (authors, journals, countries, etc.)
- Error handling ensures report generation even with partial data
"""

            # Create sheets in Excel
            for sheet_name, df in sheets_data:
                try:
                    if not df.empty:
                        ws = wb.create_sheet(sheet_name)
                        for r in dataframe_to_rows(df, index=False, header=True):
                            ws.append(r)
                    else:
                        ws = wb.create_sheet(sheet_name)
                        ws.append([f"No data available for {sheet_name}"])
                except Exception as e:
                    print(f"Error creating sheet {sheet_name}: {e}")

            # Add summary sheet
            try:
                ws_summary = wb.create_sheet('Report_Summary')
                for line in summary_content.split('\n'):
                    ws_summary.append([line])
            except Exception as e:
                print(f"Error creating summary sheet: {e}")

            # Save Excel file
            wb.save(excel_path)

            # Cleanup
            shutil.rmtree(temp_dir)

            return excel_path

        except Exception as e:
            print(f"Critical error in save_all_data_to_excel: {e}")
            # Создаем минимальный отчет в случае критической ошибки
            try:
                timestamp = int(time.time())
                excel_path = os.path.join(tempfile.gettempdir(),
                                          f"minimal_references_analysis_results_{timestamp}.xlsx")
                wb = Workbook()
                ws = wb.active
                ws.title = "Error_Report_References"
                ws.append(["ERROR REPORT - REFERENCES ANALYSIS"])
                ws.append([f"Critical error during references analysis: {str(e)}"])
                ws.append([f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"])
                ws.append(["DOIs processed:", ', '.join(doi_list)])
                wb.save(excel_path)
                return excel_path
            except:
                return "error_creating_references_report"

    def display_analysis_results(self, combined_df: pd.DataFrame, source_articles_df: pd.DataFrame,
                                 doi_list: List[str], total_references: int, unique_dois: int,
                                 all_titles: List[str]) -> None:
        """Отображает результаты анализа references"""
        try:
            print(f"\n{'=' * 80}\nREFERENCES ANALYSIS RESULTS FOR {len(doi_list)} ARTICLES\n{'=' * 80}")

            if combined_df.empty and source_articles_df.empty:
                print("No data available - generating error report")
                excel_name = self.save_all_data_to_excel(combined_df, source_articles_df, doi_list, total_references,
                                                         unique_dois, all_titles)
                print(f"\nError report archived and ready for download as: {excel_name}")
                return

            unique_df = self.get_unique_references(combined_df)
            successful_refs = len(combined_df[combined_df['error'].isna()])
            stats = self.performance_monitor.get_stats()

            print(f"Total references found: {total_references}")
            print(f"Unique DOIs: {unique_dois}")
            print(f"Total references processed: {len(combined_df)}")
            print(f"Unique references: {len(unique_df)}")
            print(f"Successful references: {successful_refs}")
            print(f"Failed references: {len(combined_df[combined_df['error'].notna()])}")
            print(
                f"Total processing time: {stats.get('elapsed_seconds', 0):.2f} seconds ({stats.get('elapsed_minutes', 0):.2f} minutes)")
            print(f"Sequential processing completed with data enhancement")

            print(f"\nReferences per article:")
            for doi in doi_list:
                ref_count = len(combined_df[combined_df['source_doi'] == doi]) if not combined_df.empty else 0
                print(f"  {doi}: {ref_count} references")

            # Показываем доступные данные с обработкой ошибок
            display_cols = ['source_doi', 'position', 'doi', 'title', 'authors_with_initials', 'year',
                            'journal_abbreviation', 'publisher', 'countries', 'citation_count_crossref',
                            'citation_count_openalex']

            pd.set_option('display.max_colwidth', 25)
            pd.set_option('display.max_rows', 50)

            if not source_articles_df.empty:
                print("\nSOURCE ARTICLES:")
                try:
                    print(source_articles_df[display_cols].head(10))
                except Exception as e:
                    print(f"Error displaying source articles: {e}")

            if not unique_df.empty:
                print("\nUNIQUE REFERENCES:")
                try:
                    print(unique_df[display_cols].head(10))
                except Exception as e:
                    print(f"Error displaying unique references: {e}")

            # Показываем анализ с обработкой ошибок
            analyses = [
                ('DUPLICATE REFERENCES', self.find_duplicate_references),
                ('COUNTRIES FREQUENCY', self.analyze_countries_frequency),
                ('YEAR DISTRIBUTION', self.analyze_year_distribution),
                ('FIVE-YEAR PERIODS', self.analyze_five_year_periods),
                ('TOP 10 AUTHORS', self.analyze_authors_frequency),
                ('TOP 10 JOURNALS', self.analyze_journals_frequency),
                ('TOP 10 AFFILIATIONS', self.analyze_affiliations_frequency)
            ]

            for title, method in analyses:
                print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")
                try:
                    result = method(combined_df)
                    if not result.empty:
                        if title == 'TOP 10 JOURNALS':
                            print(result[['journal_abbreviation', 'journal_full_name', 'frequency_total',
                                          'percentage_total', 'frequency_unique', 'percentage_unique']].head(10))
                        else:
                            print(result.head(10))
                    else:
                        print("No data available")
                except Exception as e:
                    print(f"Error in {title}: {e}")

            print(f"\n{'=' * 60}\nTOP 15 TITLE WORDS\n{'=' * 60}")
            try:
                content_freq, compound_freq, scientific_freq = self.analyze_titles(all_titles)
                content_df = pd.DataFrame(content_freq.most_common(15), columns=['Word', 'Frequency'])
                content_df['Category'] = 'Content_Words'
                content_df['Rank'] = range(1, len(content_df) + 1)
                compound_df = pd.DataFrame(compound_freq.most_common(15), columns=['Word', 'Frequency'])
                compound_df['Category'] = 'Compound_Words'
                compound_df['Rank'] = range(1, len(compound_df) + 1)
                scientific_df = pd.DataFrame(scientific_freq.most_common(15), columns=['Word', 'Frequency'])
                scientific_df['Category'] = 'Scientific_Stopwords'
                scientific_df['Rank'] = range(1, len(scientific_df) + 1)
                title_word_freq_df = pd.concat([content_df, compound_df, scientific_df], ignore_index=True)[
                    ['Category', 'Rank', 'Word', 'Frequency']]
                print(title_word_freq_df)
            except Exception as e:
                print(f"Error displaying title words: {e}")

            # Всегда генерируем Excel отчет
            excel_name = self.save_all_data_to_excel(combined_df, source_articles_df, doi_list, total_references,
                                                     unique_dois, all_titles)
            print(f"\nAll data archived and ready for download as: {excel_name}")

        except Exception as e:
            print(f"Critical error in display_analysis_results: {e}")
            # Все равно генерируем отчет
            excel_name = self.save_all_data_to_excel(combined_df, source_articles_df, doi_list, total_references,
                                                     unique_dois, all_titles)
            print(f"\nError report generated and ready for download as: {excel_name}")


def analyze_references(doi_input_text: str):
    """
    API function to analyze references for a given list of DOIs.
    """
    analyzer = CitationAnalyzer()

    doi_list = analyzer.parse_doi_input(doi_input_text)

    if not doi_list:
        print("No valid DOIs provided. Please enter at least one valid DOI.")
        return

    print("Starting sequential processing for references analysis...")
    try:
        combined_references_df, source_articles_df, total_references, unique_dois, all_titles = analyzer.process_doi_sequential(
            doi_list)
        analyzer.display_analysis_results(combined_references_df, source_articles_df, doi_list, total_references,
                                          unique_dois, all_titles)
    except Exception as e:
        print(f"Critical error during processing: {e}")
        empty_df = pd.DataFrame()
        analyzer.save_all_data_to_excel(empty_df, empty_df, doi_list, 0, 0, [])
        print("Error report generated despite processing failure.")


def analyze_citing_articles(doi_input_text: str):
    """
    API function to analyze citing articles for a given list of DOIs.
    """
    analyzer = CitationAnalyzer()

    doi_list = analyzer.parse_doi_input(doi_input_text)

    if not doi_list:
        print("No valid DOIs provided. Please enter at least one valid DOI.")
        return

    print("Starting sequential processing for citing articles analysis...")
    try:
        citing_articles_df, citing_details_df, citing_results, all_citing_titles = analyzer.process_citing_articles_sequential(
            doi_list)

        # Показываем результаты
        if citing_results:
            print(f"\n{'=' * 80}\nCITING ARTICLES ANALYSIS RESULTS\n{'=' * 80}")

            total_citations = sum(data['count'] for data in citing_results.values())
            total_citing_articles = len(citing_articles_df) if citing_articles_df is not None else 0

            print(f"Total source articles: {len(doi_list)}")
            print(f"Total citing articles found: {total_citing_articles}")
            print(f"Total citation relationships: {total_citations}")

            print(f"\nCitations per source article:")
            for doi, data in citing_results.items():
                print(f"  {doi}: {data['count']} citations")

            # Показываем примеры цитирующих статей
            if citing_articles_df is not None and not citing_articles_df.empty:
                print(f"\nFirst 10 citing articles:")
                display_cols = ['doi', 'title', 'authors_with_initials', 'year', 'journal_abbreviation',
                                'citation_count_openalex']
                print(citing_articles_df[display_cols].head(10))

            # Сохраняем полный анализ в Excel
            excel_name = analyzer.save_citation_analysis_to_excel(citing_articles_df, citing_details_df, doi_list,
                                                                  citing_results, all_citing_titles)
            print(f"\nComplete citation analysis archived and saved to: {excel_name}")
        else:
            print("No citing articles found.")

    except Exception as e:
        print(f"Critical error during processing: {e}")
        empty_df = pd.DataFrame()
        analyzer.save_citation_analysis_to_excel(empty_df, empty_df, doi_list, {}, [])
        print("Error report generated despite processing failure.")


if __name__ == "__main__":
    # Пример вызова функций API

    # --- Анализ пристатейных списков литературы ---
    print("--- Running References Analysis ---")
    doi_input_for_references = "10.1038/s41586-023-06924-6"
    analyze_references(doi_input_for_references)

    print("\n\n" + "=" * 100 + "\n\n")

    # --- Анализ цитирующих статей ---
    print("--- Running Citing Articles Analysis ---")
    doi_input_for_citing = "10.1038/s41586-023-06924-6"
    analyze_citing_articles(doi_input_for_citing)