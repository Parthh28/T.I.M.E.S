"""
URL scraper module for extracting event information from webpage links.
"""

import re
import json
import logging
from typing import Optional
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("calendar_bot.scraper")

URL_REGEX = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def find_urls(text: str) -> list[str]:
    """Find all HTTP/HTTPS URLs in a text string."""
    return URL_REGEX.findall(text)


async def scrape_event_page(url: str) -> Optional[str]:
    """
    Fetch a webpage and extract clean text and JSON-LD event schema.
    Returns condensed summary text suitable for Gemini event extraction.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    timeout_cfg = httpx.Timeout(8.0, connect=4.0)
    try:
        async with httpx.AsyncClient(timeout=timeout_cfg, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code >= 400:
                logger.warning("Scraping %s returned status %d", url, resp.status_code)
                return None
            html = resp.text
    except Exception as e:
        logger.error("Failed to fetch event URL %s: %s", url, e)
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")

        # 1. Check for JSON-LD schema (schema.org Event)
        json_ld_events = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict):
                    if data.get("@type") == "Event" or "Event" in str(data.get("@type", "")):
                        json_ld_events.append(data)
                    elif "@graph" in data and isinstance(data["@graph"], list):
                        for item in data["@graph"]:
                            if isinstance(item, dict) and (item.get("@type") == "Event" or "Event" in str(item.get("@type", ""))):
                                json_ld_events.append(item)
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and (item.get("@type") == "Event" or "Event" in str(item.get("@type", ""))):
                            json_ld_events.append(item)
            except Exception:
                pass

        # 2. Extract meta tags (og:title, og:description, etc.)
        meta_info = []
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            meta_info.append(f"Page Title: {title_tag.string.strip()}")

        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            meta_info.append(f"Event Title: {og_title['content'].strip()}")

        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            meta_info.append(f"Description: {og_desc['content'].strip()}")

        # 3. Clean page text
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
            tag.decompose()

        text_content = soup.get_text(separator=" ", strip=True)
        condensed_text = " ".join(text_content.split())[:4000]

        parts = []
        if json_ld_events:
            parts.append(f"Structured Event Data: {json.dumps(json_ld_events[:2])}")
        if meta_info:
            parts.append("\n".join(meta_info))
        if condensed_text:
            parts.append(f"Webpage Content: {condensed_text}")

        return "\n\n".join(parts)
    except Exception as e:
        logger.error("Error parsing HTML from %s: %s", url, e)
        return None
