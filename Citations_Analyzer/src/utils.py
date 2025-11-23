from crossref_commons.retrieval import get_publication_as_json

from functools import lru_cache
import re
from typing import List, Dict, Any

def validate_doi(doi: str) -> bool:
    if not doi or not isinstance(doi, str): return False
    doi = normalize_doi(doi)
    doi_pattern = r'^10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+$'
    return bool(re.match(doi_pattern, doi, re.IGNORECASE))

def normalize_doi(doi: str) -> str:
    if not doi or not isinstance(doi, str): return ""
    doi = doi.strip()
    prefixes = ['https://doi.org/', 'http://doi.org/', 'doi.org/', 'doi:', 'DOI:']
    for prefix in prefixes:
        if doi.lower().startswith(prefix.lower()):
            doi = doi[len(prefix):]
            break
    return doi.split('?')[0].split('#')[0].strip().lower()

def parse_doi_input(input_text: str, max_dois: int = 200) -> List[str]:
    if not input_text or not isinstance(input_text, str):
        print("Error: Input is empty or not a string")
        return []
    doi_pattern = r'10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+'
    dois = re.findall(doi_pattern, input_text, re.IGNORECASE)
    cleaned_dois = [normalize_doi(doi) for doi in dois if validate_doi(normalize_doi(doi))]
    unique_dois = sorted(list(set(cleaned_dois)))[:max_dois]
    if not unique_dois:
        print("Error: No valid DOIs found in the input.")
    else:
        print(f"Found {len(unique_dois)} valid and unique DOI(s).")
    return unique_dois

@lru_cache(maxsize=1000)
def extract_surname_with_initial(author_name: str) -> str:
    if not author_name or author_name in ['Unknown', 'Error']: return author_name
    clean_name = re.sub(r'[^\w\s\-\.]', ' ', author_name).strip()
    parts = clean_name.split()
    if not parts: return author_name
    surname = parts[-1]
    initial = parts[0][0].upper() if parts[0] else ''
    return f"{surname} {initial}." if initial else surname

#Avarage format parsing
def get_journal_info(crossref_data: Dict) -> Dict:
    container_title = crossref_data.get('container-title', [])
    short_title = crossref_data.get('short-container-title', [])
    full_name = container_title[0] if container_title else (short_title[0] if short_title else 'Unknown')
    abbreviation = short_title[0] if short_title else (container_title[0] if container_title else 'Unknown')
    return {'full_name': full_name, 'abbreviation': abbreviation,
            'publisher': crossref_data.get('publisher', 'Unknown')}

def get_affiliations_and_countries(openalex_data: Dict) -> tuple[List[str], str]:
    affiliations, countries = set(), set()
    for authorship in openalex_data.get('authorships', []):
        for institution in authorship.get('institutions', []):
            if name := institution.get('display_name'): affiliations.add(name)
            if code := institution.get('country_code'): countries.add(code.upper())
    return list(affiliations) or ['Unknown'], ';'.join(sorted(countries)) or 'Unknown'

