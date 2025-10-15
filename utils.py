"""Проверка валидности DOI"""
def validate_doi(self, doi: str) -> bool:
    if not doi or not isinstance(doi, str):
        self.logger.debug(f"Invalid DOI: {doi} (empty or not a string)")
        return False
    doi_pattern = r'^10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+$'
    is_valid = bool(re.match(doi_pattern, doi, re.IGNORECASE))
    if not is_valid:
        self.logger.debug(f"DOI {doi} does not match pattern")
    return is_valid

"""Нормализация DOI"""
def normalize_doi(self, doi: str) -> str:
    doi = doi.strip()
    prefixes = ['https://doi.org/', 'doi:', 'http://doi.org/']
    for prefix in prefixes:
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
    normalized_doi = doi.lower()
    self.logger.debug(f"Normalized DOI: {doi} -> {normalized_doi}")
    return normalized_doi