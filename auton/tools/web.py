"""Web search and page fetching tools via Exa.ai.

Ported from Lethe. Requires EXA_API_KEY environment variable.
"""

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)


def _get_exa_api_key() -> str | None:
    """Resolve Exa API key at call time."""
    key = os.environ.get("EXA_API_KEY", "").strip()
    return key or None


def web_search(
    query: str,
    num_results: int = 10,
    include_text: bool = False,
    category: str = "",
) -> str:
    """Search the web using Exa's AI-powered search engine.

    Returns relevant web pages with titles, URLs, and summaries.
    Use include_text=True to get full page content (uses more tokens).

    Args:
        query: Search query (natural language works best)
        num_results: Number of results to return (1-20, default: 10)
        include_text: Whether to include full page text (default: False, just summaries)
        category: Optional category filter: company, research paper, news, pdf, github, tweet

    Returns:
        JSON with search results including title, url, summary, and optionally full text
    """
    exa_api_key = _get_exa_api_key()
    if not exa_api_key:
        return json.dumps({"status": "error", "message": "EXA_API_KEY not set"}, indent=2)

    num_results = max(1, min(20, num_results))

    payload = {
        "query": query,
        "numResults": num_results,
        "type": "auto",
        "contents": {
            "text": {"maxCharacters": 2000} if include_text else False,
            "highlights": {"numSentences": 3},
            "summary": {"query": query},
        },
    }

    valid_categories = ["company", "research paper", "news", "pdf", "github", "tweet"]
    if category and category.lower() in valid_categories:
        payload["category"] = category.lower()

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                "https://api.exa.ai/search",
                headers={"x-api-key": exa_api_key, "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        return json.dumps({"status": "error", "message": f"Exa API error: {e.response.status_code}"}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Request failed: {e}"}, indent=2)

    results = []
    for item in data.get("results", []):
        result = {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "summary": item.get("summary", ""),
        }
        if item.get("highlights"):
            result["highlights"] = item["highlights"]
        if include_text and item.get("text"):
            result["text"] = item["text"]
        if item.get("publishedDate"):
            result["published"] = item["publishedDate"]
        results.append(result)

    return json.dumps({"status": "OK", "query": query, "num_results": len(results), "results": results}, indent=2)


def fetch_webpage(url: str, max_chars: int = 5000) -> str:
    """Fetch and extract text content from a webpage.

    Uses Exa's content extraction to get clean text from a URL.

    Args:
        url: The URL to fetch
        max_chars: Maximum characters to return (default: 5000)

    Returns:
        Extracted text content from the page
    """
    exa_api_key = _get_exa_api_key()
    if not exa_api_key:
        return json.dumps({"status": "error", "message": "EXA_API_KEY not set"}, indent=2)

    payload = {
        "ids": [url],
        "text": {"maxCharacters": max_chars},
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                "https://api.exa.ai/contents",
                headers={"x-api-key": exa_api_key, "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        return json.dumps({"status": "error", "message": f"Exa API error: {e.response.status_code}"}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Request failed: {e}"}, indent=2)

    results = data.get("results", [])
    if not results:
        return json.dumps({"status": "error", "message": f"Could not fetch content from {url}"}, indent=2)

    content = results[0]
    return json.dumps({
        "status": "OK",
        "url": content.get("url", url),
        "title": content.get("title", ""),
        "text": content.get("text", ""),
    }, indent=2)
