"""Default deny: every route not explicitly public needs the ingest token or an admin session."""


def all_routes(app):
    """Every route, also inside included routers (FastAPI wraps those in _IncludedRouter objects)."""
    for r in app.routes:
        sub = getattr(r, "original_router", None)
        yield from (sub.routes if sub is not None else [r])


def test_every_route_is_public_or_protected(client):
    from app.auth import is_public
    routes = list(all_routes(client.app))
    assert len(routes) > 50                       # the walk sees the routers' routes
    open_routes = {(m, r.path) for r in routes for m in getattr(r, "methods", None) or []
                   if is_public(m, r.path.replace("{document_id}", "1").replace("{version_id}", "1").replace("{code}", "S-1"))}
    # the only open ones: health, the search page, search / course / calendar / document reads, and the account pages
    assert {p for _, p in open_routes} == {
        "/v1/health", "/", "/v1/search", "/v1/courses", "/v1/courses/{code}", "/v1/courses/facets", "/v1/calendar", "/v1/documents/{document_id}",
        "/v1/documents/{document_id}/versions", "/v1/documents/{document_id}/versions/{version_id}",
        "/admin/login", "/admin/signup", "/admin/confirm", "/admin/forgot", "/admin/reset", "/admin/lang"}
    # no admin page and no ingest route is open
    assert not [p for _, p in open_routes if p.startswith("/v1/admin")]


def test_admin_api_needs_auth(client, auth):
    assert client.get("/v1/admin/sources").status_code == 401
    assert client.get("/v1/admin/sources", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/v1/admin/sources", headers=auth).status_code == 200


def test_unknown_routes_are_not_open(client):
    # a route nobody declared is still behind auth (401), never served openly
    assert client.post("/v1/whatever").status_code == 401
    assert client.get("/admin/anything", follow_redirects=False).status_code == 303


def test_public_health(client):
    assert client.get("/v1/health").json() == {"ok": True}
