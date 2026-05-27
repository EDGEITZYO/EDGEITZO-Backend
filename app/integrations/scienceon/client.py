from __future__ import annotations

import json
import httpx

from app.core.settings import settings


class ScienceOnClient:
    def __init__(self):
        self.base_url = settings.scienceon_base_url
        self.client_id = settings.scienceon_client_id
        self.token = settings.scienceon_token
        self.version = settings.scienceon_version

    def _build_common_params(self) -> dict:
        return {
            "client_id": self.client_id,
            "token": self.token,
            "version": self.version,
        }

    async def search_articles(
        self,
        query: str,
        page: int = 1,
        size: int = 10,
        search_field: str = "TI",
        start_year: int | None = None,
        end_year: int | None = None,
    ) -> str:
        params = self._build_common_params()
        search_query: dict = {search_field: query}
        if start_year is not None and end_year is not None:
            search_query["PY"] = f"{start_year}~{end_year}"
        elif start_year is not None:
            search_query["PY"] = str(start_year)
        elif end_year is not None:
            search_query["PY"] = str(end_year)

        params.update(
            {
                "action": "search",
                "target": "ARTI",
                "searchQuery": json.dumps(search_query, ensure_ascii=False),
                "sortField": "pubyear",
                "curPage": page,
                "rowCount": size,
                "include": ",".join(
                    [
                        "CN",
                        "DBCode",
                        "Title",
                        "Title2",
                        "Author",
                        "Affiliation",
                        "Abstract",
                        "Abstract2",
                        "Keyword",
                        "Keyword2",
                        "Pubyear",
                        "Pubdate",
                        "JournalName",
                        "ISSN",
                        "DOI",
                        "FulltextFlag",
                        "FulltextURL",
                        "ContentURL",
                        "Lang",
                        "PageInfo",
                    ]
                ),
            }
        )

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(self.base_url, params=params)
            response.raise_for_status()
            return response.text

    async def browse_article(self, cn: str) -> str:
        """CN으로 논문 상세 조회. CitedDocumentInfo(참고문헌) 포함."""
        params = self._build_common_params()
        params.update({"action": "browse", "target": "ARTI", "cn": cn})
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(self.base_url, params=params)
            response.raise_for_status()
            return response.text
