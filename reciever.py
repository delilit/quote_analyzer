class Reciever:
    def __init__(self, logger, rate_limit_calls=10, rate_limit_period=1):
        self.logger = logger
        self.rate_limit_calls = rate_limit_calls
        self.rate_limit_period = rate_limit_period # If we using this variable we must use it insted of 10 and 1 values in @sleep_and_retry lines.
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