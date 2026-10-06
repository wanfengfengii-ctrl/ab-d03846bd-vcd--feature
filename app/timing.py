"""Setup/hold timing audit of data signals against clock edges.

Built on :mod:`app.vcd`: the VCD is parsed with the same limits and the
audit is *total* — it either returns a complete result or raises
:class:`app.vcd.VCDError` with a locatable reason. No partial audit
results are ever produced.

Definitions (all times in femtoseconds):

* only normalized clock transitions of the requested kind (``0->1`` for
  ``rising``, ``1->0`` for ``falling``) with ``window.start <= t <
  window.end`` count as edges; same-timestamp assignments are arbitrated
  in text order before edge detection;
* the VCD must fully cover the window extended by both apertures, i.e.
  ``[window.start - setup_fs, window.end + hold_fs]``;
* the clock must remain ``0``/``1`` throughout that extended range;
* for an edge at ``t`` a data transition at ``t'`` is a setup violation
  when ``t - setup_fs < t' <= t`` and a hold violation when
  ``t <= t' < t + hold_fs`` — a transition exactly at ``t`` is both;
* a check is flagged ``unknown`` when the data signal is ``x``/``z``
  anywhere inside the aperture ``(t - setup_fs, t + hold_fs)``.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import Optional

from .vcd import INT_RE, MAX_FS, VCDError, Window, bad_request, parse

EDGE_KINDS = ("rising", "falling")
_TARGET_TRANSITION = {"rising": ("0", "1"), "falling": ("1", "0")}
UNKNOWN_VALUES = ("x", "z")


# --- form field parsing ----------------------------------------------------


def parse_edge(raw: Optional[str]) -> str:
    if raw is None:
        raise bad_request("MISSING_FIELD", "form field 'edge' is required")
    if raw not in EDGE_KINDS:
        raise bad_request(
            "INVALID_EDGE",
            "'edge' must be 'rising' or 'falling'",
            details={"edge": raw},
        )
    return raw


def parse_positive_fs(field: str, raw: Optional[str]) -> int:
    """Parse a strictly positive femtosecond form field (setupFs/holdFs)."""
    if raw is None:
        raise bad_request("MISSING_FIELD", f"form field '{field}' is required")
    text = raw.strip()
    if not INT_RE.match(text) or int(text) < 1:
        raise bad_request(
            "INVALID_PARAM",
            f"'{field}' must be a positive integer number of femtoseconds",
            details={"field": field, "value": raw},
        )
    value = int(text)
    if value > MAX_FS:
        raise bad_request(
            "OUT_OF_RANGE",
            f"'{field}' exceeds the supported femtosecond range",
            details={"field": field, "max_fs": MAX_FS},
        )
    return value


# --- per-signal views ------------------------------------------------------


class _SignalAudit:
    """Precomputed indexes over one signal's raw change events."""

    def __init__(self, events: list[tuple[int, str]]) -> None:
        self._events = events
        self._times = [t for t, _ in events]
        # Data transitions: every event that actually flips the value, kept
        # in text order. Same-instant glitches (e.g. 0->1->0 at a single
        # timestamp) survive, so brief changes inside an aperture cannot be
        # missed.
        transitions: list[tuple[int, str, str]] = []
        current = "x"  # uninitialized nets read as x
        for t, value in events:
            if value != current:
                transitions.append((t, current, value))
                current = value
        self._transitions = transitions
        self._transition_times = [t for t, _, _ in transitions]

    def value_at(self, t: int) -> str:
        """Effective value at *t* (final text-order assignment wins)."""
        idx = bisect_right(self._times, t) - 1
        return self._events[idx][1] if idx >= 0 else "x"

    def setup_violations(self, t: int, setup_fs: int) -> list[tuple[int, str, str]]:
        """Transitions in the setup aperture (t - setup_fs, t]."""
        lo = bisect_right(self._transition_times, t - setup_fs)
        hi = bisect_right(self._transition_times, t)
        return self._transitions[lo:hi]

    def hold_violations(self, t: int, hold_fs: int) -> list[tuple[int, str, str]]:
        """Transitions in the hold aperture [t, t + hold_fs)."""
        lo = bisect_left(self._transition_times, t)
        hi = bisect_left(self._transition_times, t + hold_fs)
        return self._transitions[lo:hi]

    def unknown_in_aperture(self, t: int, setup_fs: int, hold_fs: int) -> bool:
        """True when x/z appears anywhere in (t - setup_fs, t + hold_fs)."""
        # The value carried into the aperture: a change exactly at the open
        # setup boundary persists into it.
        if self.value_at(t - setup_fs) in UNKNOWN_VALUES:
            return True
        # Any change event strictly inside the aperture, including
        # zero-width glitches to x/z.
        lo = bisect_right(self._times, t - setup_fs)
        hi = bisect_left(self._times, t + hold_fs)
        return any(value in UNKNOWN_VALUES for _, value in self._events[lo:hi])


# --- clock analysis --------------------------------------------------------


def _clock_binary_violation(
    events: list[tuple[int, str]], start: int, end: int
) -> Optional[tuple[int, str]]:
    """First (time, value) at which the clock leaves 0/1 within [start, end]."""
    entry = "x"  # uninitialized nets read as x
    hit: Optional[tuple[int, str]] = None
    for t, value in events:
        if t > end:
            break
        if t <= start:
            entry = value  # final text-order value carried into the range
        if t >= start and value in UNKNOWN_VALUES and hit is None:
            hit = (t, value)
    if entry in UNKNOWN_VALUES:
        return (start, entry)
    return hit


def _clock_edges(
    events: list[tuple[int, str]], edge: str, lo: int, hi: int
) -> list[int]:
    """Times in [lo, hi) where the normalized clock makes the requested
    0->1 (rising) or 1->0 (falling) transition."""
    target = _TARGET_TRANSITION[edge]
    edges: list[int] = []
    prev = "x"  # uninitialized nets read as x
    i = 0
    while i < len(events):
        t = events[i][0]
        if t >= hi:
            break
        # Normalization: the final text-order assignment at a timestamp wins.
        final = events[i][1]
        while i + 1 < len(events) and events[i + 1][0] == t:
            i += 1
            final = events[i][1]
        if final != prev:
            if (prev, final) == target and t >= lo:
                edges.append(t)
            prev = final
        i += 1
    return edges


# --- request level processing ----------------------------------------------


def audit(
    text: str,
    *,
    clock: str,
    signals: list[str],
    window: Window,
    edge: str,
    setup_fs: int,
    hold_fs: int,
) -> dict:
    """Parse *text* and run the setup/hold audit. All-or-nothing result."""
    vcd = parse(text)

    if clock not in vcd.names:
        raise VCDError(
            "UNDECLARED_SIGNAL",
            "clock signal is not declared in the VCD",
            details={"signals": [clock]},
            http_status=400,
        )
    missing = [s for s in signals if s not in vcd.names]
    if missing:
        raise VCDError(
            "UNDECLARED_SIGNAL",
            "selected signal is not declared in the VCD",
            details={"signals": missing},
            http_status=400,
        )

    # The VCD must fully cover the setup/hold apertures around the window.
    assert vcd.first_ts is not None and vcd.last_ts is not None
    range_start = window.start - setup_fs
    range_end = window.end + hold_fs
    if range_start < vcd.first_ts or range_end > vcd.last_ts:
        raise VCDError(
            "APERTURE_NOT_COVERED",
            "the VCD does not fully cover the setup/hold apertures around "
            "the window: a timestamp at or before window start - setupFs "
            "and at or after window end + holdFs is required",
            details={
                "window_start_fs": window.start,
                "window_end_fs": window.end,
                "setup_fs": setup_fs,
                "hold_fs": hold_fs,
                "required_start_fs": range_start,
                "required_end_fs": range_end,
                "first_timestamp_fs": vcd.first_ts,
                "last_timestamp_fs": vcd.last_ts,
            },
        )

    clock_events = vcd.changes.get(vcd.names[clock], [])
    binary_hit = _clock_binary_violation(clock_events, range_start, range_end)
    if binary_hit is not None:
        t, value = binary_hit
        raise VCDError(
            "NON_BINARY_CLOCK",
            f"clock {clock!r} must remain 0 or 1 throughout the aperture "
            f"range but is {value!r} at {t} fs",
            details={"signal": clock, "time_fs": t, "value": value},
        )

    edges = _clock_edges(clock_events, edge, window.start, window.end)
    views = {
        name: _SignalAudit(vcd.changes.get(vcd.names[name], []))
        for name in signals
    }

    edge_entries: list[dict] = []
    n_setup = n_hold = n_unknown = n_clean = 0
    for t in edges:
        results = {}
        for name in sorted(signals):
            view = views[name]
            setup_hits = view.setup_violations(t, setup_fs)
            hold_hits = view.hold_violations(t, hold_fs)
            unknown = view.unknown_in_aperture(t, setup_fs, hold_fs)
            violations = [
                {"kind": "setup", "time": tt, "from": frm, "to": to}
                for tt, frm, to in setup_hits
            ] + [
                {"kind": "hold", "time": tt, "from": frm, "to": to}
                for tt, frm, to in hold_hits
            ]
            n_setup += bool(setup_hits)
            n_hold += bool(hold_hits)
            n_unknown += bool(unknown)
            n_clean += not violations and not unknown
            results[name] = {
                "sampled": view.value_at(t),
                "unknown": unknown,
                "violations": violations,
            }
        edge_entries.append({"time": t, "signals": results})

    return {
        "window": {"start": window.start, "end": window.end, "unit": "fs"},
        "clock": clock,
        "edge": edge,
        "setup_fs": setup_fs,
        "hold_fs": hold_fs,
        "edges": edge_entries,
        "summary": {
            "edges": len(edges),
            "signals": len(signals),
            "checks": len(edges) * len(signals),
            "clean": n_clean,
            "setup_violations": n_setup,
            "hold_violations": n_hold,
            "unknown": n_unknown,
        },
    }
