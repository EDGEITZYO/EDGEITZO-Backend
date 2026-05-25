from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.recent_read_service import _row_to_recent_read_item


def run() -> None:
    row = SimpleNamespace(
        paper_id="PAPER-1",
        title="Sample Paper",
        title_en="Sample Paper EN",
        abstract="Abstract",
        abstract_en=None,
        doi="10.1234/example",
        issn="1234-5678",
        pubyear=2026,
        source_type="scienceon",
        db_code="JAKO",
        journal_name="Journal of Testing",
        journal_issn=["1234-5678", "8765-4321"],
        read_at=datetime.now(timezone.utc),
    )

    item = _row_to_recent_read_item(row)

    assert item.paper_id == "PAPER-1"
    assert item.title == "Sample Paper"
    assert item.journal_name == "Journal of Testing"
    assert item.journal_issn == ["1234-5678", "8765-4321"]
    print("recent read service tests passed")


if __name__ == "__main__":
    run()
