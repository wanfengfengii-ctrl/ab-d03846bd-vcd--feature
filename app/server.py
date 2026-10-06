"""HTTP layer for the VCD windowing service."""

from __future__ import annotations

import json
import os
from typing import Optional

from flask import Flask, jsonify, request

from .vcd import (
    MAX_FILE_BYTES,
    VCDError,
    audit,
    bad_request,
    parse_edge,
    parse_positive_fs,
    parse_signals,
    parse_window,
    process,
)


def create_app() -> Flask:
    app = Flask(__name__)
    # 4 MiB file limit plus headroom for the multipart envelope; the file
    # part itself is checked precisely against MAX_FILE_BYTES.
    app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_BYTES + 1024 * 1024

    @app.errorhandler(413)
    def too_large(_err: Exception) -> tuple:
        return (
            jsonify(
                {
                    "error": {
                        "code": "FILE_TOO_LARGE",
                        "message": "uploaded VCD exceeds the 4 MiB limit",
                    }
                }
            ),
            413,
        )

    @app.get("/healthz")
    def healthz() -> tuple:
        return jsonify({"status": "ok"}), 200

    @app.post("/api/vcd/window")
    def vcd_window() -> tuple:
        try:
            return _handle_window()
        except VCDError as exc:
            body, status = exc.to_response()
            return jsonify(body), status

    def _read_upload_text() -> str:
        # Note: request.content_length includes the multipart envelope, so
        # the 4 MiB rule is enforced on the decoded file part below instead.
        if "file" not in request.files:
            raise bad_request("MISSING_FIELD", "multipart field 'file' is required")
        upload = request.files["file"]
        raw = upload.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise VCDError(
                "FILE_TOO_LARGE",
                f"uploaded VCD exceeds {MAX_FILE_BYTES} bytes (4 MiB)",
                http_status=413,
            )
        try:
            return raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise bad_request(
                "NOT_ASCII",
                "the VCD file must be ASCII encoded",
                details={"byte_offset": exc.start},
            ) from exc

    def _handle_window() -> tuple:
        text = _read_upload_text()
        window = parse_window(_form_value("window"))
        signals = parse_signals(request.form.getlist("signals"))

        result = process(text, signals, window)
        return jsonify(result), 200

    @app.post("/api/vcd/timing-audit")
    def vcd_timing_audit() -> tuple:
        try:
            return _handle_timing_audit()
        except VCDError as exc:
            body, status = exc.to_response()
            return jsonify(body), status

    def _handle_timing_audit() -> tuple:
        text = _read_upload_text()
        window = parse_window(_form_value("window"))
        clock = _form_value("clock")
        if clock is None:
            raise bad_request("MISSING_FIELD", "form field 'clock' is required")
        signals = parse_signals(request.form.getlist("signals"))
        edge = parse_edge(_form_value("edge"))
        setup_fs = parse_positive_fs(_form_value("setupFs"), "setupFs")
        hold_fs = parse_positive_fs(_form_value("holdFs"), "holdFs")

        result = audit(text, clock, signals, window, edge, setup_fs, hold_fs)
        return jsonify(result), 200

    def _form_value(key: str) -> Optional[str]:
        values = request.form.getlist(key)
        if len(values) > 1:
            raise bad_request("DUPLICATE_FIELD", f"form field {key!r} appears more than once")
        return values[0] if values else None

    @app.errorhandler(405)
    def method_not_allowed(_err: Exception) -> tuple:
        return jsonify({"error": {"code": "METHOD_NOT_ALLOWED", "message": "method not allowed"}}), 405

    @app.errorhandler(404)
    def not_found(_err: Exception) -> tuple:
        return jsonify({"error": {"code": "NOT_FOUND", "message": "not found"}}), 404

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
