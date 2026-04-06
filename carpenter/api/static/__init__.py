"""Static asset loader for API HTML pages.

Provides file-reading utilities so Python endpoint code can load
CSS, JS, and HTML templates from the static/ directory instead of
embedding them inline.
"""
from functools import lru_cache
from pathlib import Path

_STATIC_DIR = Path(__file__).parent


@lru_cache(maxsize=32)
def read_asset(filename: str) -> str:
    """Read a static asset file and return its contents as a string.

    Results are cached so repeated calls within the same process
    don't hit disk.

    Args:
        filename: Name of the file inside the static/ directory
                  (e.g. "review-diff.css").

    Returns:
        The file contents as a string.

    Raises:
        FileNotFoundError: If the asset file does not exist.
    """
    return (_STATIC_DIR / filename).read_text()


def load_template(html_file: str, **kwargs: str) -> str:
    """Load an HTML template and substitute placeholders.

    Placeholders use Python's str.format_map syntax: {key}.
    All values are passed through as-is (no auto-escaping).

    Args:
        html_file: Name of the HTML template file.
        **kwargs: Placeholder values to substitute.

    Returns:
        The rendered HTML string.
    """
    template = read_asset(html_file)
    return template.format_map(kwargs)
