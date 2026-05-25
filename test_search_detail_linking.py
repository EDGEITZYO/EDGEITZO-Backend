from app.schemas.search import PaperSearchItem
from app.services.credibility_service import calculate_credibility
from app.services.search_service import (
    _apply_detail_links,
    _candidate_detail_paper_cn,
)


def _item(
    *,
    paper_id: str,
    source: str,
) -> PaperSearchItem:
    return PaperSearchItem(
        paper_id=paper_id,
        title="Demo paper",
        source=source,
        credibility=calculate_credibility(),
        score=0.0,
    )


def _assert_equal(name: str, expected, actual) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected}, got {actual}")


def run() -> None:
    scienceon_cn = _item(paper_id="JAKO202412039603634", source="scienceon")
    scienceon_doi = _item(paper_id="10.1234/demo", source="scienceon")
    scienceon_unknown = _item(paper_id="Unknown", source="scienceon")
    semantic_item = _item(paper_id="abc123", source="semantic_scholar")

    _assert_equal(
        "scienceon cn candidate",
        "JAKO202412039603634",
        _candidate_detail_paper_cn(scienceon_cn),
    )
    _assert_equal("scienceon doi candidate", None, _candidate_detail_paper_cn(scienceon_doi))
    _assert_equal("scienceon unknown candidate", None, _candidate_detail_paper_cn(scienceon_unknown))
    _assert_equal("semantic scholar candidate", None, _candidate_detail_paper_cn(semantic_item))

    items = _apply_detail_links(
        [scienceon_cn, scienceon_doi, scienceon_unknown, semantic_item],
        {"JAKO202412039603634"},
    )

    _assert_equal("detail available", True, items[0].detail_available)
    _assert_equal("detail paper cn", "JAKO202412039603634", items[0].detail_paper_cn)
    _assert_equal("detail url", "/api/v1/papers/JAKO202412039603634", items[0].detail_url)
    _assert_equal("doi detail unavailable", False, items[1].detail_available)
    _assert_equal("unknown detail unavailable", False, items[2].detail_available)
    _assert_equal("semantic detail unavailable", False, items[3].detail_available)

    print("search detail linking tests passed")


if __name__ == "__main__":
    run()
