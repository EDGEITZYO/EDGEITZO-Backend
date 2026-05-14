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
    ) -> str:
        params = self._build_common_params()
        params.update(
            {
                "action": "search",
                "target": "ARTI",
                "searchQuery": f'{{"TI":"{query}"}}',
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