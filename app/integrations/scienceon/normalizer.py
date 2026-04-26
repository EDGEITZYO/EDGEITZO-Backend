from app.schemas.search import (
    CredibilityInfo,
    PaperAuthor,
    PaperSearchItem,
)


def _safe_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _split_semicolon_text(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in str(value).split(";") if item.strip()]


def _split_dot_text(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in str(value).split(".") if item.strip()]


def _find_records(node):
    """
    ScienceON XML 파싱 결과에서 record 리스트를 최대한 방어적으로 탐색
    """
    if isinstance(node, list):
        return node

    if not isinstance(node, dict):
        return []

    candidate_keys = [
        "record",
        "Record",
        "records",
        "Records",
        "result",
        "RESULT",
        "outputData",
        "resultSet",
        "returnObject",
    ]

    for key in candidate_keys:
        value = node.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _find_records(value)
            if nested:
                return nested

    for value in node.values():
        if isinstance(value, dict):
            nested = _find_records(value)
            if nested:
                return nested
        elif isinstance(value, list) and value:
            if isinstance(value[0], dict):
                return value

    return []


def normalize_scienceon_search_response(parsed: dict) -> list[PaperSearchItem]:
    records = _find_records(parsed)
    items: list[PaperSearchItem] = []

    for record in records:
        if not isinstance(record, dict):
            continue

        authors_raw = _split_semicolon_text(record.get("Author"))
        affiliations_raw = _split_semicolon_text(record.get("Affiliation"))
        keywords_raw = _split_dot_text(record.get("Keyword"))

        authors = []
        for idx, author_name in enumerate(authors_raw):
            affiliation = affiliations_raw[idx] if idx < len(affiliations_raw) else None
            authors.append(
                PaperAuthor(
                    name=author_name,
                    affiliation=affiliation,
                )
            )

        pubyear = record.get("Pubyear")
        try:
            year = int(pubyear) if pubyear else None
        except ValueError:
            year = None

        fulltext_flag = str(record.get("FulltextFlag", "0"))
        _ = fulltext_flag == "2"  # 필요 시 추후 필드 확장

        item = PaperSearchItem(
            paper_id=record.get("CN") or record.get("DOI") or "unknown",
            title=record.get("Title") or record.get("Title2") or "제목 없음",
            authors=authors,
            year=year,
            abstract=record.get("Abstract") or record.get("Abstract2"),
            keywords=keywords_raw,
            journal_name=record.get("JournalName"),
            source="scienceon",
            credibility=CredibilityInfo(
                badge="unknown",
                citation_count=None,
                impact_factor=None,
                kci_registered=None,
                summary="ScienceON 원본 검색 결과",
            ),
            score=0.0,
        )
        items.append(item)

    return items