from app.main import app

BASE = "/api/v1/researchers/{researcher_id}"


def paths():
    return app.openapi()["paths"]


def test_상세페이지_엔드포인트가_전부_등록돼_있다():
    registered = paths()
    for suffix in ("", "/papers", "/papers/by-year", "/coauthors", "/research-flow"):
        assert BASE + suffix in registered, f"{suffix} 누락"


def test_고정경로가_경로변수보다_먼저_선언돼_있다():
    """FastAPI는 선언 순서로 매칭한다. /{researcher_id}가 위로 올라가면
    /researchers/search 요청을 researcher_id='search'로 받아 탐색 기능이 죽는다."""
    order = [r.path for r in app.routes if hasattr(r, "path") and "/researchers" in r.path]
    variable = order.index(BASE)
    for fixed in ("/api/v1/researchers/search", "/api/v1/researchers/field-graph",
                  "/api/v1/researchers/recent-searches"):
        assert order.index(fixed) < variable, f"{fixed}가 경로변수 뒤에 있다"


def test_논문_목록_파라미터가_명세대로_열려_있다():
    params = {p["name"] for p in paths()[BASE + "/papers"]["get"]["parameters"]}
    assert {"researcher_id", "sort", "page", "size", "coauthor_id"} <= params


def test_모든_상세_엔드포인트가_404를_문서화한다():
    for suffix in ("", "/papers", "/papers/by-year", "/coauthors", "/research-flow"):
        responses = paths()[BASE + suffix]["get"]["responses"]
        assert "404" in responses, f"{suffix}에 404 응답이 문서화되지 않음"


def test_설명이_비어_있지_않다():
    for suffix in ("", "/papers", "/papers/by-year", "/coauthors", "/research-flow"):
        assert paths()[BASE + suffix]["get"].get("description"), f"{suffix} 설명 없음"
