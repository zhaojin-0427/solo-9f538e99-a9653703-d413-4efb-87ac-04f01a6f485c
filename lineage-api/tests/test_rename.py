"""Functional tests for the source-field rename API."""


def _register(client, nodes, expected=201):
    resp = client.post("/lineage/register", json={"nodes": nodes})
    assert resp.status_code == expected, resp.text
    return resp.json()


def _rename(client, old, new, expected=200):
    resp = client.post(
        "/lineage/rename", json={"old_name": old, "new_name": new}
    )
    assert resp.status_code == expected, resp.text
    return resp.json()


def _graph(client):
    resp = client.get("/lineage/graph")
    assert resp.status_code == 200
    return resp.json()


def _seed(client):
    """order_amt is referenced by a derived column and a report column."""
    _register(client, [
        {"name": "order_amt", "kind": "source"},
        {"name": "coupon_amt", "kind": "source"},
        {"name": "net_amt", "kind": "derived",
         "candidate_groups": [["order_amt", "coupon_amt"]]},
        {"name": "report_gmv", "kind": "report",
         "candidate_groups": [["net_amt"], ["order_amt"]]},
    ])


def test_rename_migrates_node_and_all_references(client):
    _seed(client)
    data = _rename(client, "order_amt", "order_amount")
    assert data["old_name"] == "order_amt"
    assert data["new_name"] == "order_amount"
    # net_amt's group + report_gmv's fallback group reference the old name.
    assert data["updated_references"] == 2

    graph = _graph(client)
    nodes = {n["name"]: n for n in graph["nodes"]}
    assert set(nodes) == {
        "order_amount", "coupon_amt", "net_amt", "report_gmv"
    }
    assert nodes["order_amount"]["kind"] == "source"
    assert nodes["net_amt"]["candidate_groups"] == [
        ["order_amount", "coupon_amt"]
    ]
    assert nodes["report_gmv"]["candidate_groups"] == [
        ["net_amt"], ["order_amount"]
    ]

    retired = {r["name"]: r["renamed_to"] for r in graph["retired_names"]}
    assert retired == {"order_amt": "order_amount"}


def test_impact_with_new_name_continues_pre_rename_result(client):
    _seed(client)
    before = client.post(
        "/lineage/impact", json={"retiring": ["order_amt"]}
    ).json()

    _rename(client, "order_amt", "order_amount")

    after = client.post(
        "/lineage/impact", json={"retiring": ["order_amount"]}
    ).json()

    def renamed(obj):
        """The pre-rename result with the old name swapped for the new one."""
        if isinstance(obj, dict):
            return {k: renamed(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [renamed(v) for v in obj]
        return "order_amount" if obj == "order_amt" else obj

    # Same columns fail, same groups, same missing inputs and root causes —
    # the failure relations carry over with the new name in place of the old.
    assert after["failed_report_columns"] == renamed(
        before["failed_report_columns"]
    )
    assert after["unavailable_nodes"] == renamed(before["unavailable_nodes"])
    assert after["unavailable_count"] == before["unavailable_count"]
    assert after["retiring"] == ["order_amount"]


def test_impact_with_retired_old_name_404_with_rename_hint(client):
    _seed(client)
    _rename(client, "order_amt", "order_amount")
    resp = client.post("/lineage/impact", json={"retiring": ["order_amt"]})
    assert resp.status_code == 404
    details = resp.json()["details"]
    assert details["unknown"] == ["order_amt"]
    assert details["renamed"] == {"order_amt": "order_amount"}


def test_rename_unknown_old_name_404_and_graph_untouched(client):
    _seed(client)
    before = _graph(client)
    resp = client.post(
        "/lineage/rename", json={"old_name": "ghost", "new_name": "x"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"
    assert _graph(client) == before


def test_rename_non_source_rejected_and_graph_untouched(client):
    _seed(client)
    before = _graph(client)
    for old, kind in (("net_amt", "derived"), ("report_gmv", "report")):
        resp = client.post(
            "/lineage/rename", json={"old_name": old, "new_name": "x_" + old}
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["details"]["kind"] == kind
    assert _graph(client) == before


def test_rename_to_occupied_name_rejected_and_graph_untouched(client):
    _seed(client)
    before = _graph(client)
    resp = client.post(
        "/lineage/rename",
        json={"old_name": "order_amt", "new_name": "coupon_amt"},
    )
    assert resp.status_code == 409
    assert "coupon_amt" in resp.json()["details"]["new_name"]
    assert _graph(client) == before


def test_rename_to_retired_name_rejected_and_graph_untouched(client):
    _seed(client)
    _rename(client, "order_amt", "order_amount")
    before = _graph(client)
    # order_amt is now retired: it cannot become the new name of another field.
    resp = client.post(
        "/lineage/rename",
        json={"old_name": "coupon_amt", "new_name": "order_amt"},
    )
    assert resp.status_code == 409
    assert _graph(client) == before


def test_rename_same_name_rejected(client):
    _seed(client)
    before = _graph(client)
    resp = client.post(
        "/lineage/rename",
        json={"old_name": "order_amt", "new_name": "order_amt"},
    )
    assert resp.status_code == 422
    assert _graph(client) == before


def test_rename_of_retired_old_name_404_and_graph_untouched(client):
    _seed(client)
    _rename(client, "order_amt", "order_amount")
    before = _graph(client)
    # The old name is retired: renaming it again must not change anything.
    resp = client.post(
        "/lineage/rename",
        json={"old_name": "order_amt", "new_name": "other"},
    )
    assert resp.status_code == 404
    assert resp.json()["details"]["renamed_to"] == "order_amount"
    assert _graph(client) == before


def test_register_creating_retired_name_rejected(client):
    _seed(client)
    _rename(client, "order_amt", "order_amount")

    resp = _register(client, [
        {"name": "order_amt", "kind": "source"},
    ], expected=409)
    assert resp["error"] == "retired_name"
    assert resp["details"]["retired_nodes"] == ["order_amt"]
    assert resp["details"]["renamed_to"] == {"order_amt": "order_amount"}

    # The old name must not flow back into the graph.
    names = {n["name"] for n in _graph(client)["nodes"]}
    assert "order_amt" not in names


def test_register_referencing_retired_name_rejected_atomically(client):
    _seed(client)
    _rename(client, "order_amt", "order_amount")

    resp = _register(client, [
        {"name": "s_new", "kind": "source"},
        {"name": "r_new", "kind": "report",
         "candidate_groups": [["order_amt", "s_new"]]},
    ], expected=409)
    assert resp["error"] == "retired_name"
    assert resp["details"]["retired_inputs"] == {"r_new": ["order_amt"]}

    # Whole batch rolled back: even the valid new source must not exist.
    names = {n["name"] for n in _graph(client)["nodes"]}
    assert "s_new" not in names
    assert "r_new" not in names


def test_register_with_new_name_still_accepted(client):
    _seed(client)
    _rename(client, "order_amt", "order_amount")
    _register(client, [
        {"name": "report_v2", "kind": "report",
         "candidate_groups": [["order_amount"]]},
    ])
    nodes = {n["name"]: n for n in _graph(client)["nodes"]}
    assert nodes["report_v2"]["candidate_groups"] == [["order_amount"]]


def test_chained_rename_retires_every_old_name(client):
    _seed(client)
    _rename(client, "order_amt", "order_amount")
    data = _rename(client, "order_amount", "order_amt_v3")
    assert data["updated_references"] == 2

    graph = _graph(client)
    retired = {r["name"]: r["renamed_to"] for r in graph["retired_names"]}
    assert retired == {
        "order_amt": "order_amount",
        "order_amount": "order_amt_v3",
    }
    nodes = {n["name"]: n for n in graph["nodes"]}
    assert nodes["net_amt"]["candidate_groups"] == [
        ["order_amt_v3", "coupon_amt"]
    ]

    # Every retired name stays rejected in later registrations.
    resp = _register(client, [
        {"name": "order_amount", "kind": "source"},
    ], expected=409)
    assert resp["error"] == "retired_name"
