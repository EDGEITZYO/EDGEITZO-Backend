from app.services.credibility_service import JournalEvidence, calculate_credibility


def _assert_badge(name: str, expected: str, actual: str) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected}, got {actual}")


def _assert_equal(name: str, expected, actual) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected}, got {actual}")


def run() -> None:
    _assert_badge(
        "citation high",
        "high",
        calculate_credibility(citation_count=50).badge,
    )
    _assert_badge(
        "citation medium",
        "medium",
        calculate_credibility(citation_count=10).badge,
    )
    _assert_badge(
        "citation low",
        "low",
        calculate_credibility(citation_count=9).badge,
    )
    _assert_badge(
        "sjr high",
        "high",
        calculate_credibility(
            journal=JournalEvidence(sjr_quartile="Q1", sjr_score=2.3),
        ).badge,
    )
    _assert_badge(
        "sjr medium",
        "medium",
        calculate_credibility(
            journal=JournalEvidence(sjr_quartile="Q3", sjr_score=0.6),
        ).badge,
    )
    _assert_badge(
        "journal metadata medium",
        "medium",
        calculate_credibility(journal_name="Sample Journal").badge,
    )
    _assert_badge(
        "no evidence unknown",
        "unknown",
        calculate_credibility().badge,
    )

    indexed_info = calculate_credibility(
        citation_count=12,
        journal=JournalEvidence(
            sci_indexed=True,
            kci_indexed=False,
            impact_factor=3.4,
        ),
    )
    _assert_equal("sci badge", "SCI O", indexed_info.sci_badge)
    _assert_equal("kci badge", "KCI X", indexed_info.kci_badge)
    _assert_equal("citation badge", "Citations 12", indexed_info.citation_badge)
    _assert_equal("impact factor badge", "IF 3.4", indexed_info.impact_factor_badge)

    unknown_info = calculate_credibility(citation_count=None)
    _assert_equal("unknown sci badge", "SCI unknown", unknown_info.sci_badge)
    _assert_equal("unknown kci badge", "KCI unknown", unknown_info.kci_badge)
    _assert_equal("unknown citation badge", None, unknown_info.citation_badge)
    print("credibility service tests passed")


if __name__ == "__main__":
    run()
