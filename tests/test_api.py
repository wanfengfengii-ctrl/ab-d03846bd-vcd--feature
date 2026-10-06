"""HTTP-level tests for POST /api/vcd/window."""

from __future__ import annotations

import io
import json

from app.server import create_app
from tests.conftest import build_vcd


def make_client():
    return create_app().test_client()


def post_vcd(client, vcd_text, signals, start=0, end=20_000_000, field="file"):
    data = {
        field: (io.BytesIO(vcd_text.encode("ascii")), "wave.vcd"),
        "window": json.dumps({"start": start, "end": end}),
    }
    if signals is not None:
        if isinstance(signals, str):
            data["signals"] = signals
        else:
            data["signals"] = signals
    return client.post(
        "/api/vcd/window",
        data=data,
        content_type="multipart/form-data",
    )


GOOD_VCD = build_vcd(
    "#0\n0!\n#5\n1!\n#10\n0!\n#20\n1!\n"
)


def test_healthz():
    resp = make_client().get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_window_happy_path():
    resp = post_vcd(make_client(), GOOD_VCD, ["top.clk"])
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["window"] == {"start": 0, "end": 20_000_000, "unit": "fs"}
    assert body["signals"]["top.clk"] == [
        {"start": 0, "end": 5_000_000, "value": "0"},
        {"start": 5_000_000, "end": 10_000_000, "value": "1"},
        {"start": 10_000_000, "end": 20_000_000, "value": "0"},
    ]


def test_multiple_signals_sorted_keys():
    vcd = build_vcd("#0\n0!\n1\"\n#10\n1!\n0\"\n")
    resp = post_vcd(make_client(), vcd, ["top.data", "top.clk"], end=10_000_000)
    assert resp.status_code == 200
    body = resp.get_json()
    assert list(body["signals"].keys()) == ["top.clk", "top.data"]


def test_signals_as_json_array():
    resp = post_vcd(
        make_client(), GOOD_VCD, json.dumps(["top.clk"])
    )
    assert resp.status_code == 200


def test_missing_file():
    client = make_client()
    resp = client.post(
        "/api/vcd/window",
        data={"window": json.dumps({"start": 0, "end": 100})},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "MISSING_FIELD"


def test_missing_window():
    client = make_client()
    data = {"file": (io.BytesIO(GOOD_VCD.encode()), "w.vcd"),
            "signals": ["top.clk"]}
    resp = client.post("/api/vcd/window", data=data,
                       content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "MISSING_FIELD"


def test_duplicate_signals_400():
    resp = post_vcd(make_client(), GOOD_VCD, ["top.clk", "top.clk"])
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "DUPLICATE_SIGNAL"


def test_undeclared_signal_400():
    resp = post_vcd(make_client(), GOOD_VCD, ["top.nope"])
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "UNDECLARED_SIGNAL"
    assert "top.nope" in err["details"]["signals"]


def test_syntax_error_422_with_line():
    vcd = build_vcd("#0\n0!\n#5\n!!!bogus\n")
    resp = post_vcd(make_client(), vcd, ["top.clk"])
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "VCD_SYNTAX_ERROR"
    assert isinstance(err.get("line"), int)


def test_time_regression_422():
    vcd = build_vcd("#10\n0!\n#5\n1!\n")
    resp = post_vcd(make_client(), vcd, ["top.clk"])
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "TIME_REGRESSION"


def test_window_not_covered_422_no_partial_results():
    resp = post_vcd(make_client(), GOOD_VCD, ["top.clk"], start=0, end=999_000_000)
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "WINDOW_NOT_COVERED"


def test_non_ascii_rejected():
    text = GOOD_VCD.encode() + b"\xff\xfe"
    client = make_client()
    data = {
        "file": (io.BytesIO(text), "w.vcd"),
        "window": json.dumps({"start": 0, "end": 20_000_000}),
        "signals": ["top.clk"],
    }
    resp = client.post("/api/vcd/window", data=data,
                       content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "NOT_ASCII"


def test_file_too_large_rejected():
    # Pad a valid-looking body over 4 MiB inside a comment.
    padding = "$comment " + ("x " * (2 * 1024 * 1024 + 10)) + " $end\n"
    vcd = build_vcd(padding + "#0\n0!\n")
    resp = post_vcd(make_client(), vcd, ["top.clk"])
    assert resp.status_code == 413
    assert resp.get_json()["error"]["code"] == "FILE_TOO_LARGE"


def test_exact_size_boundary():
    from app.vcd import MAX_FILE_BYTES

    base = build_vcd("#0\n0!\n#10\n0!\n")
    marker = "$enddefinitions $end\n"
    assert base.count(marker) == 1

    def sized(n_bytes: int) -> bytes:
        # Inserted block right after the header:
        # "$comment " (9) + k*"y" + " $end\n" (6)
        block_overhead = 15
        k = n_bytes - len(base.encode()) - block_overhead
        block = "$comment " + "y" * k + " $end\n"
        text = base.replace(marker, marker + block, 1)
        return text.encode("ascii")

    data = sized(MAX_FILE_BYTES)
    assert len(data) == MAX_FILE_BYTES, len(data)
    client = make_client()
    resp = client.post(
        "/api/vcd/window",
        data={
            "file": (io.BytesIO(data), "w.vcd"),
            "window": json.dumps({"start": 0, "end": 10_000_000}),
            "signals": ["top.clk"],
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)

    over = sized(MAX_FILE_BYTES + 1)
    assert len(over) > MAX_FILE_BYTES
    resp = client.post(
        "/api/vcd/window",
        data={
            "file": (io.BytesIO(over), "w.vcd"),
            "window": json.dumps({"start": 0, "end": 10_000_000}),
            "signals": ["top.clk"],
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 413


def test_same_timestamp_and_cross_timescale_over_http():
    vcd_ns = build_vcd(
        "#0\n0!\n#10\n0!\n1!\nx!\n1!\n#20\n0!\n",
        timescale="1ns",
    )
    vcd_ps = build_vcd(
        "#0\n0!\n#10000\n0!\n1!\nx!\n1!\n#20000\n0!\n",
        timescale="1ps",
    )
    window = {"start": 0, "end": 20_000_000}
    client = make_client()

    def call(text):
        data = {
            "file": (io.BytesIO(text.encode()), "w.vcd"),
            "window": json.dumps(window),
            "signals": ["top.clk"],
        }
        return client.post("/api/vcd/window", data=data,
                           content_type="multipart/form-data")

    r1 = call(vcd_ns)
    r2 = call(vcd_ps)
    assert r1.status_code == r2.status_code == 200
    assert r1.get_json()["signals"] == r2.get_json()["signals"]
    sig = r1.get_json()["signals"]["top.clk"]
    assert sig == [
        {"start": 0, "end": 10_000_000, "value": "0"},
        {"start": 10_000_000, "end": 20_000_000, "value": "1"},
    ]


# --- POST /api/vcd/timing-audit ---------------------------------------------

AUDIT_VCD = build_vcd(
    "#0\n0!\n0\"\n"
    "#10\n1!\n"
    "#20\n0!\n"
    "#26\n1\"\n"
    "#30\n1!\n"
    "#33\n0\"\n"
    "#40\n0!\n"
    "#50\n1!\n"
    "#60\n0!\n"
)


def post_audit(client, vcd_text, clock="top.clk", signals=("top.data",),
               start=10_000_000, end=50_000_000, edge="rising",
               setup=5_000_000, hold=5_000_000):
    data = {
        "file": (io.BytesIO(vcd_text.encode("ascii")), "wave.vcd"),
        "window": json.dumps({"start": start, "end": end}),
        "edge": edge,
        "setupFs": str(setup),
        "holdFs": str(hold),
    }
    if clock is not None:
        data["clock"] = clock
    if signals is not None:
        data["signals"] = list(signals)
    return client.post(
        "/api/vcd/timing-audit",
        data=data,
        content_type="multipart/form-data",
    )


def test_audit_happy_path():
    resp = post_audit(make_client(), AUDIT_VCD)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["window"] == {"start": 10_000_000, "end": 50_000_000, "unit": "fs"}
    assert body["clock"] == "top.clk"
    assert body["edge"] == "rising"
    assert body["setupFs"] == 5_000_000
    assert body["holdFs"] == 5_000_000
    assert [e["time"] for e in body["edges"]] == [10_000_000, 30_000_000]
    first, second = body["edges"]
    assert first["samples"] == {"top.data": "0"}
    assert first["violations"] == []
    assert second["samples"] == {"top.data": "1"}
    assert second["violations"] == [
        {"kind": "setup", "signal": "top.data",
         "at": 26_000_000, "from": "0", "to": "1"},
        {"kind": "hold", "signal": "top.data",
         "at": 33_000_000, "from": "1", "to": "0"},
    ]
    assert body["summary"] == {
        "edges": 2,
        "setupViolations": 1,
        "holdViolations": 1,
        "unknown": 0,
        "cleanEdges": 1,
    }


def test_audit_missing_fields_400():
    client = make_client()
    resp = post_audit(client, AUDIT_VCD, clock=None)
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "MISSING_FIELD"
    resp = post_audit(client, AUDIT_VCD, signals=None)
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "MISSING_FIELD"
    resp = client.post(
        "/api/vcd/timing-audit",
        data={"clock": "top.clk", "signals": ["top.data"]},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "MISSING_FIELD"


def test_audit_invalid_edge_400():
    resp = post_audit(make_client(), AUDIT_VCD, edge="sideways")
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "INVALID_EDGE"


def test_audit_edge_alias_normalized():
    resp = post_audit(make_client(), AUDIT_VCD, edge="PosEdge")
    assert resp.status_code == 200
    assert resp.get_json()["edge"] == "rising"


def test_audit_non_positive_aperture_400():
    for kwargs in ({"setup": 0}, {"setup": -1}, {"hold": 0}, {"hold": -2}):
        resp = post_audit(make_client(), AUDIT_VCD, **kwargs)
        assert resp.status_code == 400, kwargs
        assert resp.get_json()["error"]["code"] == "INVALID_APERTURE"


def test_audit_aperture_not_covered_422_no_partial_result():
    resp = post_audit(make_client(), AUDIT_VCD, setup=20_000_000)
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["error"]["code"] == "APERTURE_NOT_COVERED"
    assert "edges" not in body

    resp = post_audit(make_client(), AUDIT_VCD, hold=20_000_000)
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "APERTURE_NOT_COVERED"


def test_audit_non_binary_clock_422_no_partial_result():
    vcd = build_vcd("#0\n0!\n0\"\n#5\nx!\n#10\n1!\n#20\n0!\n")
    resp = post_audit(make_client(), vcd, start=5_000_000, end=15_000_000,
                      setup=2_000_000, hold=2_000_000)
    assert resp.status_code == 422
    body = resp.get_json()
    err = body["error"]
    assert err["code"] == "NON_BINARY_CLOCK"
    assert err["details"]["value"] == "x"
    assert "edges" not in body


def test_audit_vcd_syntax_error_422_with_line():
    vcd = build_vcd("#0\n0!\n#5\n!!!bogus\n")
    resp = post_audit(make_client(), vcd)
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "VCD_SYNTAX_ERROR"
    assert isinstance(err.get("line"), int)


def test_audit_undeclared_signal_400():
    resp = post_audit(make_client(), AUDIT_VCD, clock="top.nope")
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "UNDECLARED_SIGNAL"
    assert "top.nope" in err["details"]["signals"]


def test_audit_file_too_large_rejected():
    padding = "$comment " + ("x " * (2 * 1024 * 1024 + 10)) + " $end\n"
    vcd = build_vcd(padding + "#0\n0!\n")
    resp = post_audit(make_client(), vcd)
    assert resp.status_code == 413
    assert resp.get_json()["error"]["code"] == "FILE_TOO_LARGE"


def test_audit_same_instant_and_cross_timescale_over_http():
    vcd_ns = build_vcd(
        "#0\n0!\n0\"\n#10\n1!\n1\"\n#20\n0!\n0\"\n#30\n1!\n#40\n0!\n",
        timescale="1ns",
    )
    vcd_ps = build_vcd(
        "#0\n0!\n0\"\n#10000\n1!\n1\"\n#20000\n0!\n0\"\n"
        "#30000\n1!\n#40000\n0!\n",
        timescale="1ps",
    )
    client = make_client()

    def call(text):
        return post_audit(client, text, start=5_000_000, end=35_000_000,
                          setup=2_000_000, hold=2_000_000)

    r1, r2 = call(vcd_ns), call(vcd_ps)
    assert r1.status_code == r2.status_code == 200
    b1, b2 = r1.get_json(), r2.get_json()
    assert b1["edges"] == b2["edges"]
    assert b1["summary"] == b2["summary"]
    # Data toggles exactly at the 10 ns edge: both setup and hold.
    first, second = b1["edges"]
    assert first["time"] == 10_000_000
    assert [v["kind"] for v in first["violations"]] == ["setup", "hold"]
    assert all(v["at"] == 10_000_000 for v in first["violations"])
    assert second["violations"] == []
    assert b1["summary"] == {
        "edges": 2,
        "setupViolations": 1,
        "holdViolations": 1,
        "unknown": 0,
        "cleanEdges": 1,
    }
