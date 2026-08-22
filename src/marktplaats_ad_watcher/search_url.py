from __future__ import annotations

from urllib.parse import SplitResult, parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

DEFAULT_MARKTPLAATS_SORT_BY = "SORT_INDEX"
DEFAULT_MARKTPLAATS_SORT_ORDER = "DECREASING"


def normalize_marktplaats_search_url(value: str) -> str:
    """Canonicalize Marktplaats search URLs to a predictable API form.

    - ``/lrp/api/search`` URLs get explicit UI sort defaults when missing.
    - ``/q/<term>/#key:value|...`` URLs are converted to ``/lrp/api/search``.
    """

    if not isinstance(value, str):
        return value

    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return raw

    host = parsed.netloc.split(":", 1)[0].lower()
    if host != "www.marktplaats.nl":
        return raw

    path = parsed.path.rstrip("/")
    if path == "/lrp/api/search":
        return _normalize_search_endpoint(parsed)
    if parsed.path.startswith("/q/"):
        return _convert_query_route_to_search_endpoint(parsed)
    return raw


def _normalize_search_endpoint(split: SplitResult) -> str:
    params = dict(parse_qsl(split.query, keep_blank_values=True))
    params.setdefault("sortBy", DEFAULT_MARKTPLAATS_SORT_BY)
    params.setdefault("sortOrder", DEFAULT_MARKTPLAATS_SORT_ORDER)

    rebuilt = split._replace(query=urlencode(params), fragment="")
    return urlunsplit(rebuilt)


def _convert_query_route_to_search_endpoint(split: SplitResult) -> str:
    path_term = split.path[len("/q/") :].strip("/")
    params: dict[str, str] = {}

    if path_term:
        params["query"] = unquote_plus(path_term)

    for token in split.fragment.split("|"):
        key, separator, raw_value = token.partition(":")
        key = key.strip()
        if not key or not separator:
            continue
        params[key] = unquote_plus(raw_value.strip())

    params.update(dict(parse_qsl(split.query, keep_blank_values=True)))
    params.setdefault("searchInTitleAndDescription", "true")
    params.setdefault("limit", "30")
    params.setdefault("offset", "0")
    params.setdefault("sortBy", DEFAULT_MARKTPLAATS_SORT_BY)
    params.setdefault("sortOrder", DEFAULT_MARKTPLAATS_SORT_ORDER)

    rebuilt = split._replace(path="/lrp/api/search", query=urlencode(params), fragment="")
    return urlunsplit(rebuilt)