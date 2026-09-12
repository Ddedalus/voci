def test_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.json() == {"hello": "world"}


def test_top_items(client):
    r = client.get("/items")
    assert r.status_code == 200
    assert r.json() == {"top": True}


def test_users_get(client):
    r = client.get("/users/5")
    assert r.status_code == 200
    assert r.json() == {"id": 5}


def test_users_me(client):
    r = client.get("/users/me")
    assert r.status_code == 200
    assert r.json() == {"id": "me"}


def test_items_list(client):
    r = client.get("/users/1/items/")
    assert r.status_code == 200
    assert r.json() == {"items": []}


def test_items_get(client):
    r = client.get("/users/1/items/42")
    assert r.status_code == 200
    assert r.json() == {"item_id": 42}


def test_items_create(client):
    r = client.post("/users/1/items/")
    assert r.status_code == 200
    assert r.json() == {"created": True}


def test_legacy_health_currently_shadowed(client):
    # catch_all("/{name}") is registered before health("/health"), so it wins
    # today: this asserts *current* (shadowed) behavior.
    r = client.get("/legacy/health")
    assert r.status_code == 200
    assert r.json() == {"name": "health"}


def test_legacy_catch_all(client):
    r = client.get("/legacy/anything-else")
    assert r.status_code == 200
    assert r.json() == {"name": "anything-else"}


def test_admin_summary(client):
    r = client.get("/api/admin/reports/summary")
    assert r.status_code == 200
    assert r.json() == {"summary": True}


def test_admin_detail(client):
    r = client.get("/api/admin/reports/detail/7")
    assert r.status_code == 200
    assert r.json() == {"report_id": 7}


def test_multi_get(client):
    r = client.get("/multi")
    assert r.status_code == 200


def test_multi_post(client):
    r = client.post("/multi")
    assert r.status_code == 200


def test_manual(client):
    r = client.get("/manual")
    assert r.status_code == 200
    assert r.json() == {"manual": True}


def test_dynamic(client):
    r = client.get("/from-variable")
    assert r.status_code == 200
    assert r.json() == {"dynamic": True}


def test_sub_ping(client):
    r = client.get("/sub/ping")
    assert r.status_code == 200
    assert r.json() == {"ping": "pong"}


def test_validated_order_ok(client):
    r = client.post("/validated/orders", json={"quantity": 5})
    assert r.status_code == 200
    assert r.json() == {"quantity": 5}


def test_validated_order_422(client):
    # violates Field(gt=0) -- pydantic rejects before create_order() ever runs
    r = client.post("/validated/orders", json={"quantity": -3})
    assert r.status_code == 422


def test_validated_search_default(client):
    r = client.get("/validated/search")
    assert r.status_code == 200
    assert r.json() == {"limit": 10}


def test_validated_search_query(client):
    r = client.get("/validated/search", params={"limit": 3})
    assert r.status_code == 200
    assert r.json() == {"limit": 3}


def test_validated_protected_ok(client):
    r = client.get("/validated/protected", headers={"x-token": "secret"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_validated_protected_401(client):
    # Depends(require_token) raises before protected() ever runs
    r = client.get("/validated/protected", headers={"x-token": "wrong"})
    assert r.status_code == 401


def test_ws_echo(client):
    with client.websocket_connect("/ws/echo") as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "ping"


def test_404_unknown(client):
    r = client.get("/this-path-does-not-exist")
    assert r.status_code == 404


def test_405_users(client):
    r = client.delete("/users/5")
    assert r.status_code == 405


def test_405_search(client):
    r = client.delete("/validated/search")
    assert r.status_code == 405


def test_openapi_snapshot(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    assert "/users/{id}" in schema["paths"]
    assert "/validated/orders" in schema["paths"]


def test_url_path_for(app):
    assert app.url_path_for("get_user", id=5) == "/users/5"


def test_users_get_other_id_control(client):
    # control: unrelated to the edits under test, should stay green throughout
    r = client.get("/users/2")
    assert r.status_code == 200
    assert r.json() == {"id": 2}


def test_items_get_other_control(client):
    r = client.get("/users/9/items/1")
    assert r.status_code == 200
    assert r.json() == {"item_id": 1}


def test_admin_detail_other_control(client):
    r = client.get("/api/admin/reports/detail/1")
    assert r.status_code == 200


def test_405_manual(client):
    r = client.post("/manual")
    assert r.status_code == 405
