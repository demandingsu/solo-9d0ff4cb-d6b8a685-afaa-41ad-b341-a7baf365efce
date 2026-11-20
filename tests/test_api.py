"""端到端测试：用字表/字库上传、交印出单、原单复用、缺字、保存失败、并发次序。

运行：
    PYTHONPATH=. pytest -q
"""
from __future__ import annotations

import io
import os
import tempfile
import threading

import pytest

# 每个测试会话使用独立临时库
_tmp = tempfile.TemporaryDirectory()
os.environ["MATRIX_DB_PATH"] = os.path.join(_tmp.name, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import service  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    service.ensure_db()
    service.reset_all()
    with TestClient(app) as c:
        yield c


def _csv(text: str) -> bytes:
    return text.encode("utf-8-sig")


def _upload_lib(client, rows: list[tuple[str, str, str, str]]):
    buf = io.StringIO()
    buf.write("字库,版次,字模号,字\n")
    for r in rows:
        buf.write(",".join(r) + "\n")
    return client.post(
        "/api/libraries",
        files={"file": ("libraries.csv", _csv(buf.getvalue()), "text/csv")},
    )


def _upload_vol(client, name: str, chars: str):
    body = "字\n" + "\n".join(chars) + "\n"
    return client.post(
        f"/api/volumes/{name}/chars",
        files={"file": (f"{name}.csv", _csv(body), "text/csv")},
    )


def _ident(glyph: str, lib: str, edition: str, no: str) -> dict:
    return {
        "glyph_char": glyph,
        "library_name": lib,
        "edition": edition,
        "matrix_no": no,
    }


# ---------- 基础：优先级选库 ----------


def test_first_library_wins(client):
    r = _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("宋韵楼", "初刻", "SR-2", "地"),
        ("金石斋", "重刊", "JS-1", "天"),  # 同字次优先，不应被选
        ("金石斋", "重刊", "JS-2", "人"),
    ])
    assert r.status_code == 200, r.text
    _upload_vol(client, "册甲", "天地人")
    r = client.post("/api/volumes/册甲/submit")
    assert r.status_code == 200, r.text
    d = r.json()
    after = {(x["glyph_char"], x["library_name"], x["matrix_no"]) for x in d["volume_after"]}
    assert ("天", "宋韵楼", "SR-1") in after
    assert ("地", "宋韵楼", "SR-2") in after
    assert ("人", "金石斋", "JS-2") in after
    assert all(x["library_name"] != "金石斋" or x["glyph_char"] != "天" for x in d["volume_after"])
    # 首单：池从 0 增到 3，无减项
    assert len(d["added"]) == 3 and d["removed"] == []


# ---------- 核心场景：两册先后弃用同一字模 ----------


def test_two_volumes_drop_same_matrix_sequentially(client):
    _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("宋韵楼", "初刻", "SR-2", "地"),
        ("宋韵楼", "初刻", "SR-3", "人"),
    ])
    _upload_vol(client, "册甲", "天地人")
    _upload_vol(client, "册乙", "天地")

    # 册甲先交印：共需池 = 天/地/人
    o1 = client.post("/api/volumes/册甲/submit").json()
    assert {x["glyph_char"] for x in o1["pool_after"]} == {"天", "地", "人"}

    # 册乙交印：与册甲共需并集仍是 天/地/人，故无增减
    o2 = client.post("/api/volumes/册乙/submit").json()
    assert o2["added"] == [] and o2["removed"] == []
    assert {x["glyph_char"] for x in o2["pool_after"]} == {"天", "地", "人"}

    # 册甲改稿，弃用“人”：第一次弃用——池里还有乙吗？乙不用“人”，故立即减
    _upload_vol(client, "册甲", "天地")
    o3 = client.post("/api/volumes/册甲/submit").json()
    assert [x["glyph_char"] for x in o3["removed"]] == ["人"]
    assert o3["added"] == []
    assert {x["glyph_char"] for x in o3["pool_after"]} == {"天", "地"}

    # 构造“两册共用、先后弃用”：册乙改稿加入“人”，两册再共需
    _upload_vol(client, "册乙", "天地人")
    o4 = client.post("/api/volumes/册乙/submit").json()
    assert [x["glyph_char"] for x in o4["added"]] == ["人"]

    # 现在甲（天地）、乙（天地人）共用“人”只在乙；让两册都含“人”后依次弃用：
    _upload_vol(client, "册甲", "天地人")
    client.post("/api/volumes/册甲/submit")
    state = client.get("/api/state").json()
    assert {x["glyph_char"] for x in state["shared_pool"]} == {"天", "地", "人"}

    # 甲先弃“人”：乙仍需，池不变 → 无减单
    _upload_vol(client, "册甲", "天地")
    o5 = client.post("/api/volumes/册甲/submit").json()
    assert o5["removed"] == [] and o5["added"] == []
    state = client.get("/api/state").json()
    assert {x["glyph_char"] for x in state["shared_pool"]} == {"天", "地", "人"}
    # 甲自身清单确实已更新
    jia = next(v for v in state["volumes"] if v["volume"] == "册甲")
    assert {x["glyph_char"] for x in jia["active_matrices"]} == {"天", "地"}

    # 乙再弃“人”：最后一个使用者撤下 → 减单含“人”
    _upload_vol(client, "册乙", "天地")
    o6 = client.post("/api/volumes/册乙/submit").json()
    assert [x["glyph_char"] for x in o6["removed"]] == ["人"]
    assert {x["glyph_char"] for x in o6["pool_after"]} == {"天", "地"}

    # 逐单后校验：每单 pool_after 都与当时各册 active 并集一致
    for order in (o1, o2, o3, o4, o5, o6):
        assert len(order["pool_after"]) == len({
            (x["library_name"], x["edition"], x["matrix_no"]) for x in order["pool_after"]
        })
    final = client.get("/api/state").json()
    assert {x["glyph_char"] for x in final["shared_pool"]} == {"天", "地"}


# ---------- 原单复用：幂等键 / 指纹 / 按单号重取 ----------


def test_idempotency_key_reuses_same_order(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天")])
    _upload_vol(client, "册甲", "天")
    h = {"Idempotency-Key": "job-001"}
    d1 = client.post("/api/volumes/册甲/submit", headers=h).json()
    d2 = client.post("/api/volumes/册甲/submit", headers=h).json()
    assert d1["order_no"] == d2["order_no"]
    assert d1["reused"] is False and d2["reused"] is True
    # 按单号原样重取，内容一致
    d3 = client.get(f"/api/orders/{d1['order_no']}").json()
    assert d3["added"] == d1["added"] and d3["removed"] == d1["removed"]
    assert d3["volume_after"] == d1["volume_after"]


def test_duplicate_confirm_without_key_reuses_order(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天"), ("宋韵楼", "初刻", "SR-2", "地")])
    _upload_vol(client, "册甲", "天地")
    d1 = client.post("/api/volumes/册甲/submit").json()
    # 不做任何改动，重复确认同一笔
    d2 = client.post("/api/volumes/册甲/submit").json()
    assert d1["order_no"] == d2["order_no"] and d2["reused"] is True
    # 改稿后再交印，应为新单
    _upload_vol(client, "册甲", "天")
    d3 = client.post("/api/volumes/册甲/submit").json()
    assert d3["order_no"] != d1["order_no"] and d3["reused"] is False
    assert [x["glyph_char"] for x in d3["removed"]] == ["地"]


def test_order_csv_download(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天")])
    _upload_vol(client, "册甲", "天")
    d = client.post("/api/volumes/册甲/submit").json()
    r = client.get(f"/api/orders/{d['order_no']}/download")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    text = r.content.decode("utf-8-sig")
    assert d["order_no"] in text and "增" in text and "天" in text


# ---------- 缺字：沿用原清单 ----------


def test_missing_glyph_keeps_old_list(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天")])
    _upload_vol(client, "册甲", "天")
    d1 = client.post("/api/volumes/册甲/submit").json()

    _upload_vol(client, "册甲", "天人")  # “人”无字库收录
    r = client.post("/api/volumes/册甲/submit")
    assert r.status_code == 422
    err = r.json()
    assert err["type"] == "MissingGlyphError" and "人" in err["error"]

    state = client.get("/api/state").json()
    jia = next(v for v in state["volumes"] if v["volume"] == "册甲")
    # 仍沿用上次交印清单（只有“天”）
    assert [x["glyph_char"] for x in jia["active_matrices"]] == ["天"]
    # 没有产生新单
    orders = client.get("/api/orders").json()
    assert [o["order_no"] for o in orders] == [d1["order_no"]]


# ---------- 保存失败：回滚并沿用原清单 ----------


def test_save_failure_rolls_back(client):
    _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("宋韵楼", "初刻", "SR-2", "地"),
    ])
    _upload_vol(client, "册甲", "天地")
    d1 = client.post("/api/volumes/册甲/submit").json()

    _upload_vol(client, "册甲", "天")
    r = client.post("/api/volumes/册甲/submit", headers={"X-Fail-Commit": "1"})
    assert r.status_code == 500
    assert r.json()["type"] == "SaveError"

    state = client.get("/api/state").json()
    jia = next(v for v in state["volumes"] if v["volume"] == "册甲")
    assert {x["glyph_char"] for x in jia["active_matrices"]} == {"天", "地"}
    orders = client.get("/api/orders")
    assert [o["order_no"] for o in orders.json()] == [d1["order_no"]]


# ---------- 并发：多人同时交印按生效次序逐单一致 ----------


def test_concurrent_submits_serialize_and_stay_consistent(client):
    _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("宋韵楼", "初刻", "SR-2", "地"),
        ("宋韵楼", "初刻", "SR-3", "人"),
    ])
    _upload_vol(client, "册甲", "天")
    _upload_vol(client, "册乙", "地")
    _upload_vol(client, "册丙", "人")

    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(3)

    def worker(vol: str):
        try:
            barrier.wait()
            with TestClient(app) as local:
                resp = local.post(f"/api/volumes/{vol}/submit")
                assert resp.status_code == 200, resp.text
                results.append(resp.json())
        except Exception as exc:  # pragma: no cover - 测试失败路径
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(v,)) for v in ("册甲", "册乙", "册丙")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    # 三单全部生效，序号连续、单号唯一
    results.sort(key=lambda d: d["seq"])
    assert [d["seq"] for d in results] == [1, 2, 3]
    assert len({d["order_no"] for d in results}) == 3
    # 逐单累积：第 n 单 pool_after 大小 == n（三字互不相同）
    for i, d in enumerate(results, start=1):
        assert len(d["pool_after"]) == i
        assert len(d["added"]) == 1 and d["removed"] == []

    state = client.get("/api/state").json()
    assert {x["glyph_char"] for x in state["shared_pool"]} == {"天", "地", "人"}
    listing = client.get("/api/orders").json()
    assert [o["seq"] for o in listing] == [1, 2, 3]


# ---------- 其他 ----------


def test_unknown_volume_and_order(client):
    r = client.post("/api/volumes/不存在/submit")
    assert r.status_code == 404
    r = client.get("/api/orders/NOPE-9")
    assert r.status_code == 404


def test_bad_csv_headers(client):
    r = client.post(
        "/api/libraries",
        files={"file": ("x.csv", _csv("foo,bar\n1,2\n"), "text/csv")},
    )
    assert r.status_code == 400 and "缺少必需列" in r.json()["error"]


# ---------- 带页码用字表：解析、交印入档、按字模回查 ----------


def _upload_vol_paged(client, name: str, rows: list[tuple[str, str]]):
    body = "页码,字\n" + "\n".join(f"{p},{c}" for p, c in rows) + "\n"
    return client.post(
        f"/api/volumes/{name}/chars",
        files={"file": (f"{name}.csv", _csv(body), "text/csv")},
    )


def _lib3(client):
    r = _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("宋韵楼", "初刻", "SR-2", "地"),
        ("宋韵楼", "初刻", "SR-3", "人"),
    ])
    assert r.status_code == 200, r.text


def _lookup(client, lib="宋韵楼", edition="初刻", no="SR-1"):
    r = client.get(f"/api/lookup?library={lib}&edition={edition}&matrix_no={no}")
    assert r.status_code == 200, r.text
    return r.json()


def test_paged_upload_submit_and_lookup(client):
    _lib3(client)
    # 带「页码、字」两列上传；字格连写多字、同字跨页
    r = _upload_vol_paged(client, "册甲", [("一", "天地"), ("二", "人"), ("三", "天")])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["char_count"] == 3 and d["paged"] is True and d["page_count"] == 3

    o = client.post("/api/volumes/册甲/submit").json()
    assert o["has_pages"] is True
    # 逐页记录随单入档：页一 天/地、页二 人、页三 天
    pages = {pg["page_no"]: {x["glyph_char"] for x in pg["items"]} for pg in o["pages"]}
    assert pages == {"一": {"天", "地"}, "二": {"人"}, "三": {"天"}}
    # 按单号重取，逐页记录原样还在
    again = client.get(f"/api/orders/{o['order_no']}").json()
    assert again["pages"] == o["pages"]

    # 回查“天”的字模 → 该单、册甲、页一/三
    lk = _lookup(client, no="SR-1")
    assert lk["glyph_char"] == "天" and lk["match_count"] == 1
    m = lk["matches"][0]
    assert m["order_no"] == o["order_no"] and m["volume"] == "册甲"
    assert m["pages"] == ["一", "三"] and m["pages_unknown"] is False
    # 回查“人”的字模 → 页二
    lk3 = _lookup(client, no="SR-3")
    assert lk3["matches"][0]["pages"] == ["二"]


def test_pageless_order_listed_as_pages_unknown(client):
    _lib3(client)
    _upload_vol(client, "册甲", "天地")          # 无页码用字表
    o1 = client.post("/api/volumes/册甲/submit").json()
    assert o1["has_pages"] is False and o1["pages"] is None

    _upload_vol_paged(client, "册乙", [("五", "天")])  # 带页码
    o2 = client.post("/api/volumes/册乙/submit").json()

    lk = _lookup(client, no="SR-1")
    assert lk["match_count"] == 2
    by_vol = {m["volume"]: m for m in lk["matches"]}
    # 无页码单也列出，标为页码未知
    assert by_vol["册甲"]["pages"] is None and by_vol["册甲"]["pages_unknown"] is True
    assert by_vol["册乙"]["pages"] == ["五"] and by_vol["册乙"]["pages_unknown"] is False


def test_page_change_issues_new_order_even_same_matrices(client):
    _lib3(client)
    _upload_vol_paged(client, "册甲", [("一", "天"), ("二", "地")])
    o1 = client.post("/api/volumes/册甲/submit").json()

    # 换页：用字与用模完全不变，仅“地”从页二改到页三 → 必须出新单
    _upload_vol_paged(client, "册甲", [("一", "天"), ("三", "地")])
    o2 = client.post("/api/volumes/册甲/submit").json()
    assert o2["order_no"] != o1["order_no"] and o2["reused"] is False
    assert o2["added"] == [] and o2["removed"] == []          # 共需池无增减
    assert o2["volume_before"] == o2["volume_after"]          # 用模相同
    pages = {pg["page_no"]: [x["glyph_char"] for x in pg["items"]] for pg in o2["pages"]}
    assert pages == {"一": ["天"], "三": ["地"]}

    # 回查“地”：新旧两单各自页码都在档
    lk = _lookup(client, no="SR-2")
    assert [m["pages"] for m in lk["matches"]] == [["二"], ["三"]]

    # 稿件与用模都未再变 → 复用原单
    o3 = client.post("/api/volumes/册甲/submit").json()
    assert o3["order_no"] == o2["order_no"] and o3["reused"] is True


def test_add_or_drop_pages_counts_as_change(client):
    _lib3(client)
    _upload_vol(client, "册甲", "天地")                       # 无页码
    o1 = client.post("/api/volumes/册甲/submit").json()

    # 补上页码（用字、用模不变）→ 新单
    _upload_vol_paged(client, "册甲", [("一", "天"), ("一", "地")])
    o2 = client.post("/api/volumes/册甲/submit").json()
    assert o2["order_no"] != o1["order_no"] and o2["has_pages"] is True

    # 改回无页码用字表 → 仍是变化，再出新单，且该单回查时标页码未知
    _upload_vol(client, "册甲", "天地")
    o3 = client.post("/api/volumes/册甲/submit").json()
    assert o3["order_no"] not in (o1["order_no"], o2["order_no"])
    assert o3["has_pages"] is False
    lk = _lookup(client, no="SR-1")
    by_no = {m["order_no"]: m for m in lk["matches"]}
    assert by_no[o3["order_no"]]["pages_unknown"] is True
    assert by_no[o2["order_no"]]["pages"] == ["一"]
    assert by_no[o1["order_no"]]["pages_unknown"] is True

    # 再无变化 → 复用 o3
    o4 = client.post("/api/volumes/册甲/submit").json()
    assert o4["order_no"] == o3["order_no"] and o4["reused"] is True


def test_idempotency_key_retry_with_paged_table(client):
    _lib3(client)
    _upload_vol_paged(client, "册甲", [("一", "天")])
    h = {"Idempotency-Key": "paged-job-1"}
    d1 = client.post("/api/volumes/册甲/submit", headers=h).json()
    d2 = client.post("/api/volumes/册甲/submit", headers=h).json()
    assert d1["order_no"] == d2["order_no"] and d2["reused"] is True
    assert d2["pages"] == d1["pages"]


def test_failed_submit_keeps_pool_and_lookup(client):
    _lib3(client)
    _upload_vol_paged(client, "册甲", [("一", "天")])
    o1 = client.post("/api/volumes/册甲/submit").json()
    before = _lookup(client, no="SR-2")
    assert before["match_count"] == 0

    # 改稿加“地”并模拟保存失败 → 共需与回查都保持原状
    _upload_vol_paged(client, "册甲", [("一", "天"), ("九", "地")])
    r = client.post("/api/volumes/册甲/submit", headers={"X-Fail-Commit": "1"})
    assert r.status_code == 500
    after = _lookup(client, no="SR-2")
    assert after["match_count"] == 0
    lk1 = _lookup(client, no="SR-1")
    assert [m["pages"] for m in lk1["matches"]] == [["一"]]
    state = client.get("/api/state").json()
    assert {x["glyph_char"] for x in state["shared_pool"]} == {"天"}
    orders = client.get("/api/orders").json()
    assert [o["order_no"] for o in orders] == [o1["order_no"]]


def test_lookup_survives_library_replacement(client):
    _lib3(client)
    _upload_vol_paged(client, "册甲", [("一", "天"), ("二", "地")])
    o1 = client.post("/api/volumes/册甲/submit").json()

    # 换库：整套字库换成新库新号，旧字模不再生效
    _upload_lib(client, [("金石斋", "重刊", "JS-1", "天"), ("金石斋", "重刊", "JS-2", "地")])
    lk = _lookup(client, no="SR-1")
    assert lk["in_current_library"] is False
    assert lk["match_count"] == 1
    assert lk["matches"][0]["order_no"] == o1["order_no"]
    assert lk["matches"][0]["pages"] == ["一"]


def test_lookup_uses_matrix_index(client):
    _lib3(client)
    _upload_vol_paged(client, "册甲", [("一", "天")])
    client.post("/api/volumes/册甲/submit")
    from app.database import connect

    conn = connect()
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT order_no FROM order_matrix_pages "
            "WHERE library_name='宋韵楼' AND edition='初刻' AND matrix_no='SR-1'"
        ).fetchall()
        detail = " ".join(str(tuple(row)) for row in plan).lower()
        assert "idx_omp_matrix" in detail  # 按字模索引定位，不逐单扫描
        # 归档行数：带页码单按 (字模,页) 一行
        n = conn.execute("SELECT COUNT(*) c FROM order_matrix_pages").fetchone()["c"]
        assert n == 1
    finally:
        conn.close()


def test_paged_csv_validation_and_no_match(client):
    _lib3(client)
    # 页码列为空 → 400
    r = client.post(
        "/api/volumes/册甲/chars",
        files={"file": ("x.csv", _csv("页码,字\n,天\n"), "text/csv")},
    )
    assert r.status_code == 400 and "页码为空" in r.json()["error"]
    # 有页码列却没有字列 → 400
    r = client.post(
        "/api/volumes/册甲/chars",
        files={"file": ("x.csv", _csv("页码,备注\n一,天\n"), "text/csv")},
    )
    assert r.status_code == 400 and "字" in r.json()["error"]
    # 回查参数不全 → 400；无命中 → 空清单
    assert client.get("/api/lookup?library=宋韵楼").status_code == 400
    lk = _lookup(client, no="SR-9")
    assert lk["match_count"] == 0 and lk["matches"] == []


def test_order_csv_includes_page_record(client):
    _lib3(client)
    _upload_vol_paged(client, "册甲", [("一", "天"), ("二", "地")])
    o = client.post("/api/volumes/册甲/submit").json()
    text = client.get(f"/api/orders/{o['order_no']}/download").content.decode("utf-8-sig")
    assert "逐页记录" in text and "一,天,宋韵楼,初刻,SR-1" in text
