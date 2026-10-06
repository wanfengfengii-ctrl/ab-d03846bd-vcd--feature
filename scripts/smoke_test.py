#!/usr/bin/env python3
"""End-to-end HTTP smoke test for a running VCD window service.

It exercises:

* readiness via GET /healthz
* same-timestamp value changes arbitrated in text order
* a cross-timescale window (1ns vs 1ps must give identical fs timelines)
* a time-regression rejection with a locatable error (no partial result)
* timing audit: setup/hold violations around rising clock edges
* timing audit: a same-instant data/clock transition is setup *and* hold
* timing audit: cross-timescale audits (1ns vs 1ps) agree in femtoseconds
* timing audit: uncovered apertures and non-binary clocks are rejected
  with locatable errors and no partial audit results

Exits 0 only when every check passes.
"""

from __future__ import annotations

import io
import json
import os
import sys

import requests

BASE_URL = os.environ.get("VCD_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
TIMEOUT = 10

HEADER = """$timescale {ts} $end
$scope module top $end
$var wire 1 ! clk $end
$var wire 1 " rst $end
$upscope $end
$enddefinitions $end
"""

AUDIT_HEADER = """$timescale {ts} $end
$scope module top $end
$var wire 1 ! clk $end
$var wire 1 " data $end
$upscope $end
$enddefinitions $end
"""


def post(body: str, signals, start: int, end: int) -> requests.Response:
    files = {"file": ("wave.vcd", io.BytesIO(body.encode("ascii")), "text/plain")}
    data = {
        "window": json.dumps({"start": start, "end": end}),
        "signals": signals,
    }
    return requests.post(
        f"{BASE_URL}/api/vcd/window",
        files=files,
        data=data,
        timeout=TIMEOUT,
    )


def post_audit(body: str, start: int, end: int, edge: str = "rising",
               setup: int = 5_000_000, hold: int = 5_000_000) -> requests.Response:
    files = {"file": ("wave.vcd", io.BytesIO(body.encode("ascii")), "text/plain")}
    data = {
        "window": json.dumps({"start": start, "end": end}),
        "clock": "top.clk",
        "signals": ["top.data"],
        "edge": edge,
        "setupFs": str(setup),
        "holdFs": str(hold),
    }
    return requests.post(
        f"{BASE_URL}/api/vcd/timing-audit",
        files=files,
        data=data,
        timeout=TIMEOUT,
    )


failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def main() -> int:
    # 1. readiness
    try:
        r = requests.get(f"{BASE_URL}/healthz", timeout=TIMEOUT)
        check("healthz returns 200 ok",
              r.status_code == 200 and r.json().get("status") == "ok",
              f"status={r.status_code} body={r.text[:120]}")
    except requests.RequestException as exc:
        check("healthz returns 200 ok", False, str(exc))
        return 1

    # 2. same-timestamp changes: text-order arbitration + interval merge
    same_time_ns = HEADER.format(ts="1ns") + (
        "#0\n0!\n"
        "#10\n0!\n1!\nx!\n1!\n"
        "#20\n0!\n"
    )
    expected = [
        {"start": 0, "end": 10_000_000, "value": "0"},
        {"start": 10_000_000, "end": 20_000_000, "value": "1"},
    ]
    r = post(same_time_ns, ["top.clk"], 0, 20_000_000)
    timeline = r.json()["signals"]["top.clk"] if r.ok else None
    check("same-instant changes arbitrated in text order",
          r.status_code == 200 and timeline == expected,
          f"status={r.status_code} got={timeline}")

    # 3. cross-timescale window: 1ps trace of the same shape must match
    same_time_ps = HEADER.format(ts="1ps") + (
        "#0\n0!\n"
        "#10000\n0!\n1!\nx!\n1!\n"
        "#20000\n0!\n"
    )
    r2 = post(same_time_ps, ["top.clk"], 0, 20_000_000)
    timeline2 = r2.json()["signals"]["top.clk"] if r2.ok else None
    check("cross-timescale windows agree in femtoseconds",
          r2.status_code == 200 and timeline2 == timeline == expected,
          f"ns={timeline} ps={timeline2}")

    # 4. x/z coverage and multiple signals
    four_state = HEADER.format(ts="1ps") + (
        "#0\nx!\n0\"\n#1000\n0!\nz\"\n#2000\nz!\n1\"\n#3000\n1!\nx\"\n#4000\n0!\n0\"\n"
    )
    r3 = post(four_state, ["top.clk", "top.rst"], 0, 4_000_000)
    ok3 = (
        r3.status_code == 200
        and [s["value"] for s in r3.json()["signals"]["top.clk"]] == ["x", "0", "z", "1"]
        and [s["value"] for s in r3.json()["signals"]["top.rst"]] == ["0", "z", "1", "x"]
        and r3.json()["signals"]["top.clk"][0]["start"] == 0
        and r3.json()["signals"]["top.clk"][-1]["end"] == 4_000_000
    )
    check("0/1/x/z half-open intervals cover the whole window", ok3,
          f"status={r3.status_code} body={r3.text[:200]}")

    # 5. time regression rejected, locatable, no partial result
    regressing = HEADER.format(ts="1ns") + "#10\n0!\n#5\n1!\n"
    r4 = post(regressing, ["top.clk"], 0, 10_000_000)
    err = r4.json().get("error", {}) if r4.content else {}
    check("time regression rejected with a locatable error",
          r4.status_code == 422 and err.get("code") == "TIME_REGRESSION"
          and isinstance(err.get("line"), int) and "signals" not in r4.json(),
          f"status={r4.status_code} body={r4.text[:200]}")

    # 6. window outside the trace is rejected (all-or-nothing)
    r5 = post(same_time_ns, ["top.clk"], 0, 999_000_000)
    err5 = r5.json().get("error", {}) if r5.content else {}
    check("uncovered window rejected without partial results",
          r5.status_code == 422 and err5.get("code") == "WINDOW_NOT_COVERED"
          and "signals" not in r5.json(),
          f"status={r5.status_code} body={r5.text[:200]}")

    # 7. timing audit: setup and hold violations around rising edges
    audit_ns = AUDIT_HEADER.format(ts="1ns") + (
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
    r6 = post_audit(audit_ns, 10_000_000, 50_000_000)
    audit_body = r6.json() if r6.content else {}
    edges6 = audit_body.get("edges", [])
    ok6 = (
        r6.status_code == 200
        and [e["time"] for e in edges6] == [10_000_000, 30_000_000]
        and edges6[0]["samples"] == {"top.data": "0"}
        and edges6[0]["violations"] == []
        and edges6[1]["samples"] == {"top.data": "1"}
        and edges6[1]["violations"] == [
            {"kind": "setup", "signal": "top.data",
             "at": 26_000_000, "from": "0", "to": "1"},
            {"kind": "hold", "signal": "top.data",
             "at": 33_000_000, "from": "1", "to": "0"},
        ]
        and audit_body.get("summary") == {
            "edges": 2, "setupViolations": 1, "holdViolations": 1,
            "unknown": 0, "cleanEdges": 1,
        }
    )
    check("timing audit flags setup and hold violations per edge", ok6,
          f"status={r6.status_code} body={r6.text[:300]}")

    # 8. same-instant data/clock transition is both setup and hold
    same_instant = AUDIT_HEADER.format(ts="1ns") + (
        "#0\n0!\n0\"\n"
        "#10\n1!\n1\"\n"
        "#20\n0!\n0\"\n"
        "#30\n1!\n"
        "#40\n0!\n"
    )
    r7 = post_audit(same_instant, 5_000_000, 35_000_000,
                    setup=2_000_000, hold=2_000_000)
    edges7 = r7.json().get("edges", []) if r7.content else []
    ok7 = (
        r7.status_code == 200
        and [e["time"] for e in edges7] == [10_000_000, 30_000_000]
        and [v["kind"] for v in edges7[0]["violations"]] == ["setup", "hold"]
        and all(v["at"] == 10_000_000 for v in edges7[0]["violations"])
        and edges7[1]["violations"] == []
    )
    check("same-instant transition reported as setup and hold", ok7,
          f"status={r7.status_code} body={r7.text[:300]}")

    # 9. cross-timescale audit: 1ns and 1ps traces agree in femtoseconds
    audit_ps = AUDIT_HEADER.format(ts="1ps") + (
        "#0\n0!\n0\"\n"
        "#10000\n1!\n"
        "#20000\n0!\n"
        "#26000\n1\"\n"
        "#30000\n1!\n"
        "#33000\n0\"\n"
        "#40000\n0!\n"
        "#50000\n1!\n"
        "#60000\n0!\n"
    )
    r8 = post_audit(audit_ps, 10_000_000, 50_000_000)
    body8 = r8.json() if r8.content else {}
    ok8 = (
        r8.status_code == 200
        and body8.get("edges") == edges6
        and body8.get("summary") == audit_body.get("summary")
    )
    check("cross-timescale audits agree in femtoseconds", ok8,
          f"status={r8.status_code} body={r8.text[:300]}")

    # 10. aperture outside the trace is rejected (all-or-nothing)
    r9 = post_audit(audit_ns, 10_000_000, 50_000_000, setup=20_000_000)
    err9 = r9.json().get("error", {}) if r9.content else {}
    check("uncovered aperture rejected without partial audit results",
          r9.status_code == 422 and err9.get("code") == "APERTURE_NOT_COVERED"
          and "edges" not in r9.json(),
          f"status={r9.status_code} body={r9.text[:200]}")

    # 11. non-binary clock inside the aperture is rejected, locatable
    analog_clock = AUDIT_HEADER.format(ts="1ns") + (
        "#0\n0!\n0\"\n#5\nx!\n#10\n1!\n#20\n0!\n"
    )
    r10 = post_audit(analog_clock, 5_000_000, 15_000_000,
                     setup=2_000_000, hold=2_000_000)
    err10 = r10.json().get("error", {}) if r10.content else {}
    check("non-binary clock rejected with a locatable reason",
          r10.status_code == 422 and err10.get("code") == "NON_BINARY_CLOCK"
          and err10.get("details", {}).get("value") == "x"
          and "edges" not in r10.json(),
          f"status={r10.status_code} body={r10.text[:200]}")

    if failures:
        print(f"\n{len(failures)} smoke check(s) failed: {failures}")
        return 1
    print("\nall smoke checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
