from IPython.display import display, clear_output
import ipywidgets as widgets
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