from __future__ import annotations

from typing import Any

from src.product_components.news_fetcher.filter_config import normalize_keywords


def sanitize_suggestion_json(
    suggestion_json: dict[str, Any],
    *,
    headline: str,
    summary: str | None,
    existing_include_keywords: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    sanitized = dict(suggestion_json or {})
    sanitized["recommended_include_keywords"] = sanitize_recommended_include_keywords(
        sanitized.get("recommended_include_keywords"),
        headline=headline,
        summary=summary,
        existing_include_keywords=existing_include_keywords,
    )
    return sanitized


def sanitize_recommended_include_keywords(
    values: Any,
    *,
    headline: str,
    summary: str | None,
    existing_include_keywords: list[str] | tuple[str, ...],
) -> list[str]:
    article_text = _normalized_text(f"{headline}\n{summary or ''}")
    existing_keywords = normalize_keywords(existing_include_keywords)
    result: list[str] = []
    seen: set[str] = set()
    for keyword in normalize_keywords(_string_list(values)):
        if keyword in seen:
            continue
        if keyword not in article_text:
            continue
        if _is_covered_by_existing_keyword(keyword, existing_keywords):
            continue
        seen.add(keyword)
        result.append(keyword)
    return result


def _is_covered_by_existing_keyword(candidate: str, existing_keywords: tuple[str, ...]) -> bool:
    return any(existing and existing in candidate for existing in existing_keywords)


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
