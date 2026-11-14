"""Concurrency test against a real uvicorn server.

Guarantee under test: a reader can only observe a fully committed snapshot —
either the lineage before a registration batch or after it, never a partially
applied batch.
"""

import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.main import app  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Two complete, individually valid lineage snapshots.
BATCH_A = {
    "nodes": [
        {"name": "s1", "kind": "source"},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["s1"]]},
    ]
}
BATCH_B = {
    "nodes": [
        {"name": "s1", "kind": "source"},
        {"name": "s2", "kind": "source"},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["s2"]]},
    ]
}


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    db_file = tmp_path / "concurrency.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", str(db_file))
    db.init_db(str(db_file))

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            httpx.get(f"{base_url}/health", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        pytest.fail("uvicorn server did not start")

    yield base_url
    server.should_exit = True
    thread.join(timeout=5)


def test_concurrent_readers_see_only_complete_snapshots(live_server):
    base_url = live_server
    # Initial committed snapshot = BATCH_A.
    resp = httpx.post(f"{base_url}/lineage/register", json=BATCH_A, timeout=10)
    assert resp.status_code == 201

    errors: list[Exception] = []
    stop = threading.Event()

    def writer():
        # Alternate full-batch registrations; each replaces r1's groups.
        payloads = [BATCH_B, BATCH_A]
        i = 0
        while not stop.is_set():
            try:
                r = httpx.post(
                    f"{base_url}/lineage/register",
                    json=payloads[i % 2],
                    timeout=15,
                )
                assert r.status_code == 201, r.text
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)
            i += 1
            time.sleep(0.002)

    def reader_graph():
        while not stop.is_set():
            try:
                r = httpx.get(f"{base_url}/lineage/graph", timeout=15)
                assert r.status_code == 200
                nodes = {n["name"]: n for n in r.json()["nodes"]}
                # In every valid snapshot r1's groups must be exactly one of
                # the two committed definitions — never an empty/partial mix.
                assert nodes["r1"]["candidate_groups"] in (
                    [["s1"]],
                    [["s2"]],
                )
                assert set(nodes) in ({"s1", "r1"}, {"s1", "s2", "r1"})
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

    def reader_impact():
        while not stop.is_set():
            try:
                r = httpx.post(
                    f"{base_url}/lineage/impact",
                    json={"retiring": ["s1"]},
                    timeout=15,
                )
                assert r.status_code == 200
                data = r.json()
                # r1 depends on s1 in snapshot A but on s2 in B.
                failed = {c["name"] for c in data["failed_report_columns"]}
                assert failed in (set(), {"r1"})
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

    threads = [
        threading.Thread(target=writer, daemon=True),
        threading.Thread(target=reader_graph, daemon=True),
        threading.Thread(target=reader_impact, daemon=True),
    ]
    for t in threads:
        t.start()
    time.sleep(4)
    stop.set()
    for t in threads:
        t.join(timeout=10)

    assert not errors, [repr(e) for e in errors[:5]]


def test_failed_batch_leaves_no_partial_state(live_server):
    base_url = live_server
    before = httpx.get(f"{base_url}/lineage/graph").json()

    # A batch containing a cycle together with a valid new node: the whole
    # batch must be rolled back.
    bad_batch = {
        "nodes": [
            {"name": "s_new", "kind": "source"},
            {"name": "d_x", "kind": "derived",
             "candidate_groups": [["d_y"]]},
            {"name": "d_y", "kind": "derived",
             "candidate_groups": [["d_x"]]},
        ]
    }
    r = httpx.post(f"{base_url}/lineage/register", json=bad_batch, timeout=10)
    assert r.status_code == 409
    assert r.json()["error"] == "cycle_detected"

    after = httpx.get(f"{base_url}/lineage/graph").json()
    assert after == before
    assert "s_new" not in {n["name"] for n in after["nodes"]}


def test_rename_serializes_with_register(live_server):
    """Rename and registration run under the same write lock.

    Guarantees under test:
    * every observed snapshot is a complete pre- or post-rename state —
      old and new names never coexist and no dangling reference appears;
    * once the rename commits, the old name can never flow back: batches
      creating or referencing it are rejected as ``retired_name``.
    """
    base_url = live_server
    seed = {
        "nodes": [
            {"name": "s1", "kind": "source"},
            {"name": "d1", "kind": "derived",
             "candidate_groups": [["s1"]]},
            {"name": "r1", "kind": "report",
             "candidate_groups": [["d1"]]},
        ]
    }
    r = httpx.post(f"{base_url}/lineage/register", json=seed, timeout=10)
    assert r.status_code == 201

    errors: list[Exception] = []
    stop = threading.Event()
    renamed = threading.Event()

    def renamer():
        # One-shot rename; retried only on transient lock contention.
        while not stop.is_set() and not renamed.is_set():
            try:
                r = httpx.post(
                    f"{base_url}/lineage/rename",
                    json={"old_name": "s1", "new_name": "s1_v2"},
                    timeout=15,
                )
                if r.status_code == 503:
                    time.sleep(0.01)
                    continue
                assert r.status_code == 200, r.text
                renamed.set()
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

    def registrar():
        # Batches that create / reference the OLD name: valid before the
        # rename commits, rejected as retired_name afterwards. Never any
        # other outcome.
        batch = {
            "nodes": [
                {"name": "s1", "kind": "source"},
                {"name": "r2", "kind": "report",
                 "candidate_groups": [["s1"]]},
            ]
        }
        while not stop.is_set():
            try:
                r = httpx.post(
                    f"{base_url}/lineage/register", json=batch, timeout=15
                )
                assert r.status_code in (201, 409), r.text
                if r.status_code == 409:
                    assert r.json()["error"] == "retired_name", r.text
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)
            time.sleep(0.002)

    def reader():
        while not stop.is_set():
            try:
                g = httpx.get(f"{base_url}/lineage/graph", timeout=15).json()
                nodes = {n["name"]: n for n in g["nodes"]}
                # Referential integrity in every snapshot: no dangling input.
                for node in g["nodes"]:
                    for grp in node["candidate_groups"]:
                        for inp in grp:
                            assert inp in nodes, g
                # Old and new name never coexist in one snapshot.
                assert not ({"s1", "s1_v2"} <= set(nodes)), g
                # d1's only input is whichever name is currently committed.
                if "d1" in nodes:
                    assert nodes["d1"]["candidate_groups"] in (
                        [["s1"]],
                        [["s1_v2"]],
                    ), g
                # Once s1 is retired it must never appear again.
                if any(rn["name"] == "s1" for rn in g["retired_names"]):
                    assert "s1" not in nodes, g
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

    threads = [
        threading.Thread(target=renamer, daemon=True),
        threading.Thread(target=registrar, daemon=True),
        threading.Thread(target=reader, daemon=True),
    ]
    for t in threads:
        t.start()
    # Run until the rename has landed plus a margin of concurrent traffic.
    deadline = time.time() + 15
    while not renamed.is_set() and time.time() < deadline:
        time.sleep(0.05)
    assert renamed.is_set(), "rename never committed"
    time.sleep(2)
    stop.set()
    for t in threads:
        t.join(timeout=10)

    assert not errors, [repr(e) for e in errors[:5]]

    # Final committed state: the rename fully landed, no old-name residue.
    final = httpx.get(f"{base_url}/lineage/graph", timeout=10).json()
    names = {n["name"] for n in final["nodes"]}
    assert "s1_v2" in names
    assert "s1" not in names
    for node in final["nodes"]:
        for grp in node["candidate_groups"]:
            assert "s1" not in grp
    assert any(rn["name"] == "s1" for rn in final["retired_names"])

    # Impact via the new name continues the pre-rename failure relations:
    # d1 and r1 (and any r2 registered before the rename) are unavailable.
    data = httpx.post(
        f"{base_url}/lineage/impact",
        json={"retiring": ["s1_v2"]},
        timeout=10,
    ).json()
    assert {"s1_v2", "d1", "r1"} <= set(data["unavailable_nodes"])
    assert "r1" in {c["name"] for c in data["failed_report_columns"]}
