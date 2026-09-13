from app.main import app


def test_researcher_routes_are_registered_in_openapi():
    schema = app.openapi()
    paths = schema["paths"]

    assert "/api/v1/researchers/search" in paths
    assert "/api/v1/researchers/field-graph" in paths
    assert "/api/v1/researchers/recent-searches" in paths

    search_params = {
        param["name"]
        for param in paths["/api/v1/researchers/search"]["get"]["parameters"]
    }
    graph_params = {
        param["name"]
        for param in paths["/api/v1/researchers/field-graph"]["get"]["parameters"]
    }

    assert {"query", "page", "size"} <= search_params
    assert {"query", "limit"} <= graph_params
    assert "get" in paths["/api/v1/researchers/recent-searches"]
    assert "post" in paths["/api/v1/researchers/recent-searches"]
