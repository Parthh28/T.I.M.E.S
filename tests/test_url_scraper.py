"""
Unit tests for the URL scraper module.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from url_scraper import find_urls, scrape_event_page


def _run_coroutine_simple(coro):
    """Synchronously advance a non-blocking coroutine to completion."""
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    finally:
        coro.close()


def test_find_urls():
    text1 = "Check this event out: https://lu.ma/tech-meetup-2026 hope to see you there!"
    urls = find_urls(text1)
    assert len(urls) == 1
    assert urls[0] == "https://lu.ma/tech-meetup-2026"

    text2 = "No link here"
    assert find_urls(text2) == []

    text3 = "Two links: http://example.com/e1 and https://eventbrite.com/e2"
    urls3 = find_urls(text3)
    assert len(urls3) == 2
    assert "http://example.com/e1" in urls3
    assert "https://eventbrite.com/e2" in urls3


def test_scrape_event_page_json_ld():
    html_sample = """
    <html>
      <head>
        <title>Tech Summit 2026</title>
        <script type="application/ld+json">
        {
          "@type": "Event",
          "name": "Tech Summit 2026",
          "startDate": "2026-11-15T09:00:00+05:30",
          "endDate": "2026-11-15T17:00:00+05:30",
          "description": "Annual Technology Innovation Summit"
        }
        </script>
      </head>
      <body>
        <h1>Tech Summit 2026</h1>
        <p>Join us for keynotes and networking.</p>
      </body>
    </html>
    """

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = html_sample

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        content = _run_coroutine_simple(scrape_event_page("https://example.com/event"))
        assert content is not None
        assert "Tech Summit 2026" in content
        assert "2026-11-15" in content
        assert "Structured Event Data" in content


def test_scrape_event_page_failure():
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        content = _run_coroutine_simple(scrape_event_page("https://example.com/404"))
        assert content is None
