import httpx

from app.core.settings import settings


class SemanticScholarClient:
    def __init__(self):
        self.base_url = settings.semantic_scholar_base_url
        self.api_key = settings.semantic_scholar_api_key

    def _headers(self) -> dict:
        headers = {}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    async def search_papers(self, query: str, limit: int = 10) -> dict:
        url = f"{self.base_url}/paper/search"
        params = {
            "query": query,
            "limit": limit,
            "fields": ",".join(
                [
                    "paperId",
                    "title",
                    "abstract",
                    "year",
                    "citationCount",
                    "venue",
                    "journal",
                    "authors",
                ]
            ),
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(url, params=params, headers=self._headers())

            # 429면 빈 결과 반환
            if response.status_code == 429:
                return {
                    "data": [],
                    "rate_limited": True,
                }

            response.raise_for_status()
            payload = response.json()
            payload["rate_limited"] = False
            return payload
