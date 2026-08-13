from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urljoin

import httpx

from marktplaats_ad_watcher.models import Ad


class MarktplaatsParseError(RuntimeError):
    pass


class MarktplaatsClient:
    def __init__(self, *, timeout_seconds: float, user_agent: str) -> None:
        self._timeout_seconds = timeout_seconds
        self._headers = {
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            "User-Agent": user_agent,
        }

    async def fetch_ads(self, search_url: str, *, limit: int) -> list[Ad]:
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            follow_redirects=True,
            headers=self._headers,
        ) as client:
            response = await client.get(search_url)
            response.raise_for_status()

        payload = _payload_from_response(response)
        return normalize_ads(payload)[:limit]


def normalize_ads(payload: Any) -> list[Ad]:
    ads: list[Ad] = []
    seen_ids: set[str] = set()

    for raw_listing in _candidate_listing_dicts(payload):
        try:
            ad = _normalize_ad(raw_listing)
        except ValueError:
            continue

        if ad.id in seen_ids:
            continue

        seen_ids.add(ad.id)
        ads.append(ad)

    return ads


def _payload_from_response(response: httpx.Response) -> Any:
    content_type = response.headers.get("content-type", "").lower()
    text = response.text.strip()

    if "json" in content_type or text.startswith(("{", "[")):
        return response.json()

    next_data_match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(?P<payload>.*?)</script>',
        response.text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if next_data_match:
        return json.loads(html.unescape(next_data_match.group("payload")))

    raise MarktplaatsParseError(
        "Could not parse Marktplaats response. Prefer copying the lrp/api/search JSON URL "
        "from the browser Network tab."
    )


def _candidate_listing_dicts(payload: Any) -> Iterable[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def walk(current: Any) -> None:
        if isinstance(current, dict):
            if _listing_score(current) >= 4:
                candidates.append(current)
            for child in current.values():
                walk(child)
        elif isinstance(current, list):
            for child in current:
                walk(child)

    walk(payload)
    return candidates


def _listing_score(value: dict[str, Any]) -> int:
    keys = {key.lower() for key in value}
    score = 0

    if keys & {"id", "itemid", "item_id", "listingid", "listing_id", "adid", "ad_id"}:
        score += 2
    if keys & {"title", "name"}:
        score += 2
    if keys & {"vipurl", "url", "link", "href"}:
        score += 1
    if keys & {"description", "subtitle", "body"}:
        score += 1
    if keys & {"price", "priceinfo", "price_info"}:
        score += 1
    if keys & {"pictures", "images", "photos"}:
        score += 1

    return score


def _normalize_ad(raw_listing: dict[str, Any]) -> Ad:
    ad_id = _find_first_string(
        raw_listing, ["itemId", "item_id", "listingId", "listing_id", "adId", "ad_id", "id"]
    )
    title = _find_first_string(raw_listing, ["title", "name"])
    url = _find_first_string(raw_listing, ["vipUrl", "url", "link", "href"])

    if not ad_id or not title or not url:
        raise ValueError("Listing does not contain the minimum ad fields.")

    return Ad(
        id=ad_id,
        title=title,
        url=urljoin("https://www.marktplaats.nl", url),
        description=_find_first_string(raw_listing, ["description", "subtitle", "body"]),
        price=_format_price(raw_listing),
        location=_format_location(raw_listing),
        seller=_format_seller(raw_listing),
        image_urls=_collect_image_urls(raw_listing),
        raw=raw_listing,
    )


def _find_first_string(current: Any, names: list[str]) -> str | None:
    normalized_names = {name.lower() for name in names}

    if isinstance(current, dict):
        for key, value in current.items():
            if key.lower() in normalized_names and isinstance(value, str | int | float):
                result = str(value).strip()
                if result:
                    return result

        for value in current.values():
            result = _find_first_string(value, names)
            if result:
                return result

    if isinstance(current, list):
        for value in current:
            result = _find_first_string(value, names)
            if result:
                return result

    return None


def _find_first_dict(current: Any, names: list[str]) -> dict[str, Any] | None:
    normalized_names = {name.lower() for name in names}

    if isinstance(current, dict):
        for key, value in current.items():
            if key.lower() in normalized_names and isinstance(value, dict):
                return value

        for value in current.values():
            result = _find_first_dict(value, names)
            if result is not None:
                return result

    if isinstance(current, list):
        for value in current:
            result = _find_first_dict(value, names)
            if result is not None:
                return result

    return None


def _find_first_list(current: Any, names: list[str]) -> list[Any] | None:
    normalized_names = {name.lower() for name in names}

    if isinstance(current, dict):
        for key, value in current.items():
            if key.lower() in normalized_names and isinstance(value, list):
                return value

        for value in current.values():
            result = _find_first_list(value, names)
            if result is not None:
                return result

    if isinstance(current, list):
        for value in current:
            result = _find_first_list(value, names)
            if result is not None:
                return result

    return None


def _format_price(raw_listing: dict[str, Any]) -> str | None:
    price_text = _find_first_string(raw_listing, ["formattedPrice", "price", "priceText"])
    if price_text:
        return price_text

    price_info = _find_first_dict(raw_listing, ["priceInfo", "price_info"])
    if not price_info:
        return None

    cents = price_info.get("priceCents") or price_info.get("price_cents")
    if isinstance(cents, int | float):
        return f"EUR {cents / 100:.2f}"

    return None


def _format_location(raw_listing: dict[str, Any]) -> str | None:
    location = _find_first_dict(raw_listing, ["location", "sellerLocation", "seller_location"])
    if not location:
        return _find_first_string(raw_listing, ["location", "cityName", "city"])

    parts: list[str] = []
    for key in ["cityName", "city", "regionName", "region", "countryName", "country"]:
        value = location.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in parts:
            parts.append(value.strip())

    distance_meters = location.get("distanceMeters") or location.get("distance_meters")
    if isinstance(distance_meters, int | float):
        parts.append(f"{distance_meters / 1000:.1f} km")

    return ", ".join(parts) if parts else None


def _format_seller(raw_listing: dict[str, Any]) -> str | None:
    seller_info = _find_first_dict(raw_listing, ["sellerInformation", "seller", "seller_info"])
    if seller_info:
        for key in ["sellerName", "name", "displayName", "display_name"]:
            value = seller_info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return _find_first_string(raw_listing, ["sellerName", "displayName"])


def _collect_image_urls(raw_listing: dict[str, Any]) -> list[str]:
    containers = [
        container
        for container in [
            _find_first_list(raw_listing, ["pictures"]),
            _find_first_list(raw_listing, ["images"]),
            _find_first_list(raw_listing, ["photos"]),
        ]
        if container is not None
    ]

    urls: list[str] = []
    seen: set[str] = set()

    for container in containers:
        for url in _walk_strings(container):
            if not url.startswith(("http://", "https://")):
                continue
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)

    direct_image_url = _find_first_string(raw_listing, ["imageUrl", "image_url", "thumbnailUrl"])
    if (
        direct_image_url
        and direct_image_url.startswith(("http://", "https://"))
        and direct_image_url not in seen
    ):
        urls.append(direct_image_url)

    return urls


def _walk_strings(current: Any) -> Iterable[str]:
    if isinstance(current, str):
        stripped = current.strip()
        if stripped:
            yield stripped
    elif isinstance(current, dict):
        for value in current.values():
            yield from _walk_strings(value)
    elif isinstance(current, list):
        for value in current:
            yield from _walk_strings(value)
