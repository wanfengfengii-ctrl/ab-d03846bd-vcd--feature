"""Tests for the setup/hold timing audit (app.timing + HTTP endpoint)."""

from __future__ import annotations

import io
import json

import pytest

from app.server import create_app
from app.timing import audit, parse_edge, parse_positive_fs
from app.vcd import MAX_FS, VCDError, Window
from tests.conftest import build_vcd

NS = 1_000_000  # 1 ns in femtoseconds (default fixture timescale is 1ns)

# Clock rising at 10ns and 30ns, falling at 20ns and 40ns; data toggles
# safely for the first edge but inside both apertures of the second.
BASE_TRACE = build_vcd(
    "#0\n0!\n0\"\n"
    "#10\n1!\n"
    "#13\n1\"\n"
    "#20\n0!\n"
    "#25\n0\"\n"
    "#29\n1\"\n"
    "#30\n1!\n"
    "#31\n0\"\n"
    "#40\n0!\n"
    "#50\n"
)


def run_audit(vcd_text, **kwargs):
    params = dict(
        clock="top.clk",
        signals=["top.data"],
        window=Window(10 * NS, 40 * NS),
        edge="rising",
        setup_fs=2 * NS,
        hold_fs=2 * NS,
    )
    params.update(kwargs)
    return audit(vcd_text, **params)


# --- happy path ------------------------------------------------------------


def test_audit_happy_path():
    result = run_audit(BASE_TRACE)
    assert result["window"] == {"start": 10 * NS, "end": 40 * NS, "unit": "fs"}
    assert result["clock"] == "top.clk"
    assert result["edge"] == "rising"
    assert result["setup_fs"] == 2 * NS
    assert result["hold_fs"] == 2 * NS
    # Edges are reported in time order; the edge exactly at window start
    # counts, the one exactly at window end does not.
    assert [e["time"] for e in result["edges"]] == [10 * NS, 30 * NS]

    first, second = result["edges"]
    assert first["signals"]["top.data"] == {
        "sampled": "0",
        "unknown": False,
        "violations": [],
    }
    sig = second["signals"]["top.data"]
    assert sig["sampled"] == "1"
    assert sig["unknown"] is False
    assert sig["violations"] == [
        {"kind": "setup", "time": 29 * NS, "from": "0", "to": "1"},
        {"kind": "hold", "time": 31 * NS, "from": "1", "to": "0"},
    ]
    assert result["summary"] == {
        "edges": 2,
        "signals": 1,
        "checks": 2,
        "clean": 1,
        "setup_violations": 1,
        "hold_violations": 1,
        "unknown": 0,
    }


def test_falling_edge_selection():
    result = run_audit(BASE_TRACE, edge="falling")
    # Falling edges at 20ns and 40ns; 40ns is the (excluded) window end.
    assert [e["time"] for e in result["edges"]] == [20 * NS]


def test_no_edges_in_window():
    result = run_audit(BASE_TRACE, window=Window(12 * NS, 20 * NS))
    assert result["edges"] == []
    assert result["summary"] == {
        "edges": 0,
        "signals": 1,
        "checks": 0,
        "clean": 0,
        "setup_violations": 0,
        "hold_violations": 0,
        "unknown": 0,
    }


def test_multiple_signals_sorted_and_counted_independently():
    vcd = build_vcd(
        "#0\n0!\n0\"\n0#\n"
        "#10\n1!\n"
        "#20\n0!\n"
        "#29\n1\"\n"          # top.data violates setup of edge@30
        "#30\n1!\n"
        "#40\n0!\n"
        "#50\n",
        signals={"!": "clk", '"': "data", "#": "ctrl"},
    )
    result = run_audit(vcd, signals=["top.data", "top.ctrl"])
    edge30 = result["edges"][1]
    assert list(edge30["signals"].keys()) == ["top.ctrl", "top.data"]
    assert edge30["signals"]["top.ctrl"]["violations"] == []
    assert [v["kind"] for v in edge30["signals"]["top.data"]["violations"]] == ["setup"]
    summary = result["summary"]
    assert summary["signals"] == 2
    assert summary["checks"] == 4
    assert summary["clean"] == 3
    assert summary["setup_violations"] == 1


# --- aperture semantics ------------------------------------------------------


def test_same_instant_transition_is_setup_and_hold():
    vcd = build_vcd(
        "#0\n0!\n0\"\n"
        "#10\n1!\n"
        "#20\n0!\n"
        "#30\n1!\n1\"\n"       # data flips exactly at the edge
        "#40\n0!\n"
        "#50\n"
    )
    result = run_audit(vcd)
    sig = result["edges"][1]["signals"]["top.data"]
    assert sig["violations"] == [
        {"kind": "setup", "time": 30 * NS, "from": "0", "to": "1"},
        {"kind": "hold", "time": 30 * NS, "from": "0", "to": "1"},
    ]
    assert sig["sampled"] == "1"
    assert result["summary"]["setup_violations"] == 1
    assert result["summary"]["hold_violations"] == 1


def test_aperture_boundaries_are_open():
    # Changes exactly at t-setupFs and t+holdFs are outside the apertures.
    vcd = build_vcd(
        "#0\n0!\n0\"\n"
        "#10\n1!\n"
        "#20\n0!\n"
        "#28\n1\"\n"           # exactly t - setupFs for the edge at 30ns
        "#30\n1!\n"
        "#32\n0\"\n"           # exactly t + holdFs for the edge at 30ns
        "#40\n0!\n"
        "#50\n"
    )
    result = run_audit(vcd)
    sig = result["edges"][1]["signals"]["top.data"]
    assert sig["violations"] == []
    assert sig["unknown"] is False
    assert sig["sampled"] == "1"
    assert result["summary"]["clean"] == 2


def test_zero_time_glitch_inside_aperture_is_not_missed():
    vcd = build_vcd(
        "#0\n0!\n0\"\n"
        "#10\n1!\n"
        "#20\n0!\n"
        "#29\n1\"\n0\"\n"      # 0->1->0 glitch at a single timestamp
        "#30\n1!\n"
        "#40\n0!\n"
        "#50\n"
    )
    result = run_audit(vcd)
    sig = result["edges"][1]["signals"]["top.data"]
    assert sig["violations"] == [
        {"kind": "setup", "time": 29 * NS, "from": "0", "to": "1"},
        {"kind": "setup", "time": 29 * NS, "from": "1", "to": "0"},
    ]
    assert result["summary"]["setup_violations"] == 1


def test_brief_pulse_between_edges_is_not_missed():
    vcd = build_vcd(
        "#0\n0!\n0\"\n"
        "#10\n1!\n"
        "#20\n0!\n"
        "#28\n1\"\n"
        "#29\n0\"\n"           # 1ns-wide pulse ending inside the aperture
        "#30\n1!\n"
        "#40\n0!\n"
        "#50\n"
    )
    result = run_audit(vcd)
    sig = result["edges"][1]["signals"]["top.data"]
    assert sig["violations"] == [
        {"kind": "setup", "time": 29 * NS, "from": "1", "to": "0"},
    ]


def test_x_transition_inside_aperture_marks_unknown():
    vcd = build_vcd(
        "#0\n0!\n0\"\n"
        "#10\n1!\n"
        "#20\n0!\n"
        "#29\nx\"\n"
        "#30\n1!\n"
        "#40\n0!\n"
        "#50\n"
    )
    result = run_audit(vcd)
    sig = result["edges"][1]["signals"]["top.data"]
    assert sig["unknown"] is True
    assert sig["sampled"] == "x"
    assert [v["kind"] for v in sig["violations"]] == ["setup"]
    assert result["summary"]["unknown"] == 1
    assert result["summary"]["clean"] == 1


def test_constant_x_data_is_unknown_without_violations():
    vcd = build_vcd(
        "#0\n0!\nx\"\n"
        "#10\n1!\n"
        "#20\n0!\n"
        "#30\n1!\n"
        "#40\n0!\n"
        "#50\n"
    )
    result = run_audit(vcd)
    for edge in result["edges"]:
        sig = edge["signals"]["top.data"]
        assert sig["unknown"] is True
        assert sig["violations"] == []
        assert sig["sampled"] == "x"
    assert result["summary"]["unknown"] == 2
    assert result["summary"]["clean"] == 0


def test_cross_timescale_audits_match_in_femtoseconds():
    ns = build_vcd(
        "#0\n0!\n0\"\n#10\n1!\n#13\n1\"\n#20\n0!\n#25\n0\"\n"
        "#29\n1\"\n#30\n1!\n#31\n0\"\n#40\n0!\n#50\n",
        timescale="1ns",
    )
    ps = build_vcd(
        "#0\n0!\n0\"\n#10000\n1!\n#13000\n1\"\n#20000\n0!\n#25000\n0\"\n"
        "#29000\n1\"\n#30000\n1!\n#31000\n0\"\n#40000\n0!\n#50000\n",
        timescale="1ps",
    )
    assert run_audit(ns) == run_audit(ps)


# --- clock requirements ------------------------------------------------------


def test_clock_x_inside_aperture_range_rejected():
    vcd = build_vcd(
        "#0\n0!\n0\"\n#10\n1!\n#15\nx!\n#20\n0!\n#30\n1!\n#40\n0!\n#50\n"
    )
    with pytest.raises(VCDError) as exc:
        run_audit(vcd)
    assert exc.value.code == "NON_BINARY_CLOCK"
    assert exc.value.details["time_fs"] == 15 * NS
    assert exc.value.details["value"] == "x"


def test_clock_z_inside_aperture_range_rejected():
    vcd = build_vcd("#0\n0!\n0\"\n#10\n1!\n#20\nz!\n#30\n1!\n#40\n0!\n#50\n")
    with pytest.raises(VCDError) as exc:
        run_audit(vcd)
    assert exc.value.code == "NON_BINARY_CLOCK"
    assert exc.value.details["time_fs"] == 20 * NS


def test_uninitialized_clock_rejected():
    vcd = build_vcd("#0\n0\"\n#10\n1\"\n#50\n")
    with pytest.raises(VCDError) as exc:
        run_audit(vcd)
    assert exc.value.code == "NON_BINARY_CLOCK"


def test_clock_x_before_aperture_range_is_ok():
    # x->0 settles before the extended range starts at 8ns.
    vcd = build_vcd(
        "#0\nx!\n0\"\n#5\n0!\n#10\n1!\n#20\n0!\n#30\n1!\n#40\n0!\n#50\n"
    )
    result = run_audit(vcd)
    assert [e["time"] for e in result["edges"]] == [10 * NS, 30 * NS]


def test_only_normalized_clock_transition_counts_as_edge():
    # Same-instant 0->1->0 collapses to no transition: no edge at 30ns.
    vcd = build_vcd(
        "#0\n0!\n0\"\n#10\n1!\n#20\n0!\n#30\n1!\n0!\n#40\n0!\n#50\n"
    )
    result = run_audit(vcd)
    assert [e["time"] for e in result["edges"]] == [10 * NS]


# --- coverage ----------------------------------------------------------------


def test_aperture_not_covered_at_trace_end():
    vcd = build_vcd("#0\n0!\n0\"\n#10\n1!\n#20\n0!\n#30\n1!\n#40\n0!\n#41\n")
    with pytest.raises(VCDError) as exc:
        run_audit(vcd)
    assert exc.value.code == "APERTURE_NOT_COVERED"
    details = exc.value.details
    assert details["required_end_fs"] == 42 * NS
    assert details["last_timestamp_fs"] == 41 * NS


def test_aperture_not_covered_at_trace_start():
    vcd = build_vcd("#9\n0!\n0\"\n#10\n1!\n#20\n0!\n#30\n1!\n#40\n0!\n#50\n")
    with pytest.raises(VCDError) as exc:
        run_audit(vcd)
    assert exc.value.code == "APERTURE_NOT_COVERED"
    details = exc.value.details
    assert details["required_start_fs"] == 8 * NS
    assert details["first_timestamp_fs"] == 9 * NS


def test_aperture_extending_before_time_zero_rejected():
    vcd = build_vcd("#0\n0!\n0\"\n#10\n1!\n#20\n0!\n#50\n")
    with pytest.raises(VCDError) as exc:
        run_audit(vcd, window=Window(0, 10 * NS), setup_fs=1, hold_fs=1)
    assert exc.value.code == "APERTURE_NOT_COVERED"
    assert exc.value.details["required_start_fs"] == -1


# --- field parsing -----------------------------------------------------------


def test_parse_edge():
    assert parse_edge("rising") == "rising"
    assert parse_edge("falling") == "falling"
    for bad in [None, "", "both", "posedge", "Rising"]:
        with pytest.raises(VCDError) as exc:
            parse_edge(bad)
        assert exc.value.code in ("MISSING_FIELD", "INVALID_EDGE")


def test_parse_positive_fs():
    assert parse_positive_fs("setupFs", " 250 ") == 250
    for bad in [None, "", "0", "-1", "1.5", "1e3", "abc", "010"]:
        with pytest.raises(VCDError) as exc:
            parse_positive_fs("setupFs", bad)
        assert exc.value.code in ("MISSING_FIELD", "INVALID_PARAM")
    with pytest.raises(VCDError) as exc:
        parse_positive_fs("holdFs", str(MAX_FS + 1))
    assert exc.value.code == "OUT_OF_RANGE"
    assert parse_positive_fs("holdFs", str(MAX_FS)) == MAX_FS


# --- HTTP level ----------------------------------------------------------------


def make_client():
    return create_app().test_client()


def post_audit(client, vcd_text, **overrides):
    data = {
        "file": (io.BytesIO(vcd_text.encode("ascii")), "wave.vcd"),
        "window": json.dumps({"start": 10 * NS, "end": 40 * NS}),
        "clock": "top.clk",
        "signals": ["top.data"],
        "edge": "rising",
        "setupFs": str(2 * NS),
        "holdFs": str(2 * NS),
    }
    for key, value in overrides.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return client.post(
        "/api/vcd/timing-audit",
        data=data,
        content_type="multipart/form-data",
    )


def test_http_happy_path():
    resp = post_audit(make_client(), BASE_TRACE)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert [e["time"] for e in body["edges"]] == [10 * NS, 30 * NS]
    assert body["summary"]["setup_violations"] == 1
    assert body["summary"]["hold_violations"] == 1


def test_http_same_instant_and_cross_timescale():
    ns = build_vcd(
        "#0\n0!\n0\"\n#10\n1!\n#20\n0!\n#30\n1!\n1\"\n#40\n0!\n#50\n",
        timescale="1ns",
    )
    ps = build_vcd(
        "#0\n0!\n0\"\n#10000\n1!\n#20000\n0!\n#30000\n1!\n1\"\n#40000\n0!\n#50000\n",
        timescale="1ps",
    )
    client = make_client()
    r1 = post_audit(client, ns)
    r2 = post_audit(client, ps)
    assert r1.status_code == r2.status_code == 200
    assert r1.get_json() == r2.get_json()
    sig = r1.get_json()["edges"][1]["signals"]["top.data"]
    assert [v["kind"] for v in sig["violations"]] == ["setup", "hold"]
    assert all(v["time"] == 30 * NS for v in sig["violations"])


def test_http_missing_fields():
    client = make_client()
    for field in ["clock", "edge", "setupFs", "holdFs", "window", "signals"]:
        resp = post_audit(client, BASE_TRACE, **{field: None})
        assert resp.status_code == 400, field
        assert resp.get_json()["error"]["code"] == "MISSING_FIELD", field


def test_http_missing_file():
    client = make_client()
    resp = client.post(
        "/api/vcd/timing-audit",
        data={"window": json.dumps({"start": 0, "end": 100})},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "MISSING_FIELD"


def test_http_invalid_edge():
    resp = post_audit(make_client(), BASE_TRACE, edge="both")
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "INVALID_EDGE"


def test_http_invalid_setup_hold():
    client = make_client()
    for field in ["setupFs", "holdFs"]:
        for bad in ["0", "-5", "abc", "1.5"]:
            resp = post_audit(client, BASE_TRACE, **{field: bad})
            assert resp.status_code == 400, (field, bad)
            assert resp.get_json()["error"]["code"] == "INVALID_PARAM"


def test_http_duplicate_field_rejected():
    resp = post_audit(make_client(), BASE_TRACE, setupFs=["100", "200"])
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "DUPLICATE_FIELD"


def test_http_undeclared_clock_and_signal():
    client = make_client()
    resp = post_audit(client, BASE_TRACE, clock="top.nope")
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "UNDECLARED_SIGNAL"
    assert "top.nope" in err["details"]["signals"]

    resp = post_audit(client, BASE_TRACE, signals=["top.nope"])
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "UNDECLARED_SIGNAL"


def test_http_non_binary_clock_no_partial_result():
    vcd = build_vcd("#0\n0!\n0\"\n#10\n1!\n#15\nx!\n#20\n0!\n#30\n1!\n#40\n0!\n#50\n")
    resp = post_audit(make_client(), vcd)
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["error"]["code"] == "NON_BINARY_CLOCK"
    assert body["error"]["details"]["time_fs"] == 15 * NS
    assert "edges" not in body and "summary" not in body


def test_http_aperture_not_covered_no_partial_result():
    vcd = build_vcd("#0\n0!\n0\"\n#10\n1!\n#20\n0!\n#30\n1!\n#40\n0!\n#41\n")
    resp = post_audit(make_client(), vcd)
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["error"]["code"] == "APERTURE_NOT_COVERED"
    assert "edges" not in body and "summary" not in body


def test_http_vcd_error_is_locatable_and_total():
    vcd = build_vcd("#0\n0!\n0\"\n#10\n1!\n#5\n0!\n")
    resp = post_audit(make_client(), vcd)
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "TIME_REGRESSION"
    assert isinstance(err.get("line"), int)
    assert "edges" not in resp.get_json()

    vcd = build_vcd("#0\n0!\n#5\n!!!bogus\n")
    resp = post_audit(make_client(), vcd)
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "VCD_SYNTAX_ERROR"


def test_http_file_limits_shared_with_window_endpoint():
    padding = "$comment " + ("x " * (2 * 1024 * 1024 + 10)) + " $end\n"
    vcd = build_vcd(padding + "#0\n0!\n")
    resp = post_audit(make_client(), vcd)
    assert resp.status_code == 413
    assert resp.get_json()["error"]["code"] == "FILE_TOO_LARGE"

    resp = post_audit(
        make_client(), BASE_TRACE,
        file=(io.BytesIO(BASE_TRACE.encode() + b"\xff"), "wave.vcd"),
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "NOT_ASCII"
