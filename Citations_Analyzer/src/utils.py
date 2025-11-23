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