"""Normalize ScienceON parsed keyword fields."""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime
from html import unescape
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT_PATH = PROJECT_ROOT / "data" / "parsed" / "scienceon_enriched.json"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "parsed" / "scienceon_keywords_normalized.json"

HANGUL_RE = re.compile(r"[가-힣]")
LATIN_RE = re.compile(r"[A-Za-z\u00c0-\u024f]")
TOKEN_RE = re.compile(r"\S+")
BRACKET_RE = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")
SPLIT_RE = re.compile(r"\s+[.·|]\s+|\s*/\s+|\s+/\s*|;\s*|,\s+")


def _has_hangul(value: str) -> bool:
    return bool(HANGUL_RE.search(value))


def _has_latin(value: str) -> bool:
    return bool(LATIN_RE.search(value))


def _clean_term(value: object) -> str | None:
    cleaned = unescape(str(value or ""))
    cleaned = unicodedata.normalize("NFKC", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.strip(" \t\r\n;,.")
    return cleaned or None


def _dedupe(values: Iterable[str], *, lower: bool = False) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []

    for value in values:
        cleaned = _clean_term(value)
        if not cleaned:
            continue

        normalized = cleaned.casefold() if lower else cleaned
        key = normalized.casefold()
        if key in seen:
            continue

        seen.add(key)
        result.append(normalized if lower else cleaned)

    return result


def _as_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _split_obvious_terms(value: object) -> list[str]:
    cleaned = _clean_term(value)
    if not cleaned:
        return []
    return [part for part in (_clean_term(part) for part in SPLIT_RE.split(cleaned)) if part]


def _is_neutral_code(token: str) -> bool:
    body = token.strip("()[]{}.,;:")
    ascii_body = re.sub(r"[^A-Za-z0-9]", "", body)
    if not ascii_body:
        return True
    if len(ascii_body) <= 3:
        return True
    if ascii_body.isupper() and len(ascii_body) <= 8:
        return True
    if re.fullmatch(r"\d+[A-Za-z]+", ascii_body):
        return True
    return bool(re.fullmatch(r"(?:[A-Z][a-z]?\d*){1,4}", ascii_body))


def _token_language(token: str) -> str:
    if _has_hangul(token):
        return "ko"
    if _has_latin(token):
        return "neutral" if _is_neutral_code(token) else "en"
    return "neutral"


def _split_language_runs(value: str) -> tuple[list[str], list[str]]:
    tokens = TOKEN_RE.findall(value)
    runs: list[tuple[str, list[str]]] = []
    current_lang: str | None = None
    current_tokens: list[str] = []
    pending_neutral: list[str] = []

    for token in tokens:
        lang = _token_language(token)
        if lang == "neutral":
            if current_lang is None:
                pending_neutral.append(token)
            else:
                current_tokens.append(token)
            continue

        if current_lang is None:
            current_lang = lang
            current_tokens = pending_neutral + [token]
            pending_neutral = []
            continue

        if lang == current_lang:
            current_tokens.append(token)
            continue

        runs.append((current_lang, current_tokens))
        current_lang = lang
        current_tokens = pending_neutral + [token]
        pending_neutral = []

    if current_lang is None:
        return ([value], []) if _has_hangul(value) else ([], [value])

    current_tokens.extend(pending_neutral)
    runs.append((current_lang, current_tokens))

    ko_terms = [" ".join(parts) for lang, parts in runs if lang == "ko"]
    en_terms = [" ".join(parts) for lang, parts in runs if lang == "en"]
    return ko_terms, en_terms


def _extract_bracketed_terms(value: str) -> tuple[str, list[str], list[str]]:
    ko_terms: list[str] = []
    en_terms: list[str] = []

    def replace(match: re.Match[str]) -> str:
        inner = _clean_term(match.group(1) or match.group(2))
        if not inner:
            return " "

        has_hangul = _has_hangul(inner)
        has_latin = _has_latin(inner)
        if has_hangul and has_latin:
            split_ko, split_en = _split_language_runs(inner)
            ko_terms.extend(split_ko)
            en_terms.extend(split_en)
            return " "
        if has_hangul:
            ko_terms.append(inner)
            return " "
        if has_latin:
            en_terms.append(inner)
            return " "

        return match.group(0)

    without_bracketed = BRACKET_RE.sub(replace, value)
    return _clean_term(without_bracketed) or "", ko_terms, en_terms


def _split_by_language(value: object) -> tuple[list[str], list[str]]:
    ko_terms: list[str] = []
    en_terms: list[str] = []

    for term in _split_obvious_terms(value):
        has_hangul = _has_hangul(term)
        has_latin = _has_latin(term)

        if has_hangul and not has_latin:
            ko_terms.append(term)
            continue
        if has_latin and not has_hangul:
            en_terms.append(term)
            continue

        without_bracketed, bracketed_ko, bracketed_en = _extract_bracketed_terms(term)
        ko_terms.extend(bracketed_ko)
        en_terms.extend(bracketed_en)

        if not without_bracketed:
            continue
        if _has_hangul(without_bracketed) and _has_latin(without_bracketed):
            split_ko, split_en = _split_language_runs(without_bracketed)
            ko_terms.extend(split_ko)
            en_terms.extend(split_en)
        elif _has_hangul(without_bracketed):
            ko_terms.append(without_bracketed)
        elif _has_latin(without_bracketed):
            en_terms.append(without_bracketed)

    return _dedupe(ko_terms), _dedupe(en_terms, lower=True)


def normalize_paper_keywords(paper: dict) -> tuple[dict, bool]:
    normalized = deepcopy(paper)
    ko_terms: list[str] = []
    en_terms: list[str] = []

    for keyword in _as_list(paper.get("Keyword")):
        ko_part, en_part = _split_by_language(keyword)
        ko_terms.extend(ko_part)
        en_terms.extend(en_part)

    for keyword in _as_list(paper.get("Keyword2")):
        ko_part, en_part = _split_by_language(keyword)
        ko_terms.extend(ko_part)
        en_terms.extend(en_part)

    normalized["Keyword"] = _dedupe(ko_terms)
    normalized["Keyword2"] = _dedupe(en_terms, lower=True) or None

    changed = (
        normalized.get("Keyword") != paper.get("Keyword")
        or normalized.get("Keyword2") != paper.get("Keyword2")
    )
    return normalized, changed


def _count_mixed_keyword_items(papers: list[dict]) -> int:
    count = 0
    for paper in papers:
        for keyword in _as_list(paper.get("Keyword")):
            cleaned = _clean_term(keyword)
            if cleaned and _has_hangul(cleaned) and _has_latin(cleaned):
                count += 1
    return count


def _count_english_only_in_keyword(papers: list[dict]) -> int:
    count = 0
    for paper in papers:
        for keyword in _as_list(paper.get("Keyword")):
            cleaned = _clean_term(keyword)
            if cleaned and _has_latin(cleaned) and not _has_hangul(cleaned):
                count += 1
    return count


def _keyword_item_count(papers: list[dict], field: str) -> int:
    return sum(len(_as_list(paper.get(field))) for paper in papers)


def normalize_data(data: dict) -> tuple[dict, dict]:
    papers = data.get("papers")
    if not isinstance(papers, list):
        raise ValueError("Input JSON must contain a 'papers' list.")

    before_mixed = _count_mixed_keyword_items(papers)
    before_english_only = _count_english_only_in_keyword(papers)
    before_keyword_count = _keyword_item_count(papers, "Keyword")
    before_keyword2_count = _keyword_item_count(papers, "Keyword2")

    normalized_papers: list[dict] = []
    changed_count = 0
    for paper in papers:
        normalized_paper, changed = normalize_paper_keywords(paper)
        normalized_papers.append(normalized_paper)
        changed_count += int(changed)

    output = deepcopy(data)
    output["papers"] = normalized_papers
    output.setdefault("meta", {})
    output["meta"]["keyword_normalized_at"] = datetime.now().isoformat()

    stats = {
        "total_papers": len(normalized_papers),
        "papers_changed": changed_count,
        "keyword_items_before": before_keyword_count,
        "keyword2_items_before": before_keyword2_count,
        "keyword_items_after": _keyword_item_count(normalized_papers, "Keyword"),
        "keyword2_items_after": _keyword_item_count(normalized_papers, "Keyword2"),
        "mixed_keyword_items_before": before_mixed,
        "english_only_keyword_items_before": before_english_only,
        "mixed_keyword_items_after": _count_mixed_keyword_items(normalized_papers),
        "english_only_keyword_items_after": _count_english_only_in_keyword(normalized_papers),
    }
    output["meta"]["keyword_normalization"] = stats
    return output, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize ScienceON parsed keyword fields.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite the input file instead of writing a separate output file.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = args.input.resolve()
    output_path = input_path if args.in_place else args.output.resolve()

    if not input_path.exists():
        print(f"[error] input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(input_path.read_text(encoding="utf-8-sig"))
    normalized_data, stats = normalize_data(data)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(normalized_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"input: {input_path}")
    print(f"output: {output_path}")
    for key, value in stats.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
