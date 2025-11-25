from crossref_commons.retrieval import get_publication_as_json
from typing import List, Dict, Any

import requests
from typing import Dict
from datetime import datetime
from ratelimit import limits, sleep_and_retry
from tenacity import retry, stop_after_attempt, wait_exponential
from habanero import Crossref
import logging

class Config:
    REQUEST_TIMEOUT = 30
    MAX_RETRIES = 3
    MAX_WORKERS = 15  # Количество одновременных потоков для запросов

class APIClient:
    REQUEST_TIMEOUT = 30

    def __init__(self):
        self.crossref_cache = {}
        self.openalex_cache = {}
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
    
    def get_references_from_crossref(self, doi: str) -> List[Dict[str, Any]]:
        """
        Возвращает список словарей 'reference' из Crossref (если имеются).
        Использует кэш self.crossref_cache (если данные уже в нём, пытается взять оттуда).
        Возвращает пустой список при ошибке.
        """
        try:
            # Если у нас уже есть сообщение crossref в кэше — попробуем взять оттуда
            if doi in self.crossref_cache and isinstance(self.crossref_cache[doi], dict) and 'reference' in self.crossref_cache[doi]:
                return self.crossref_cache[doi].get('reference', [])

            # crossref_commons.get_publication_as_json работает с DOI
            article_data = get_publication_as_json(doi)
            refs = article_data.get('reference', []) if isinstance(article_data, dict) else []
            # Пополнить/обновить кэш минимально (чтобы не дергать API повторно)
            # Обёртка: если у нас раньше был объект cr.works, ничего не портим, иначе записываем
            if doi not in self.crossref_cache or not self.crossref_cache[doi]:
                self.crossref_cache[doi] = {'reference': refs}
            else:
                # если там уже есть dict с сообщением, добавим поле reference
                if isinstance(self.crossref_cache[doi], dict):
                    self.crossref_cache[doi]['reference'] = refs
            return refs
        except Exception:
            # при ошибке возвращаем пустой список и не ломаем обработку
            return []

