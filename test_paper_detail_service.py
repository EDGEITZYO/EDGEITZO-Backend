from app.services.paper_service import (
    _build_author_display,
    _doi_to_original_url,
    _merge_issns,
)


def _assert_equal(name: str, expected, actual) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected}, got {actual}")


def run() -> None:
    _assert_equal("doi url", "https://doi.org/10.1234/demo", _doi_to_original_url("10.1234/demo"))
    _assert_equal("doi passthrough", "https://example.com/paper", _doi_to_original_url("https://example.com/paper"))
    _assert_equal("no authors", None, _build_author_display([]))
    _assert_equal("one author", "Kim", _build_author_display(["Kim"]))
    _assert_equal("many authors", "Kim 외 2인", _build_author_display(["Kim", "Lee", "Park"]))
    _assert_equal("issn merge", ["1234-5678", "8765-4321"], _merge_issns(["1234-5678"], "8765-4321", "1234-5678"))
    print("paper detail service tests passed")


if __name__ == "__main__":
    run()
