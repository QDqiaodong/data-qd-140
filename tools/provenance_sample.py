#!/usr/bin/env python3
"""Generate and trace the boundary-size provenance sample.

Emits a valid provenance document that simultaneously reaches 32 buses,
16 devices, 64 distinct sources, 128 links per frame and 256 slots, then runs
the exact Fraction trace in streaming mode (each slot report is validated and
released as produced) and prints the per-source conservation check.

Usage:
    python3 tools/provenance_sample.py [document.json [report.json]]

When a report path is given, only the cross-slot summary section (devices and
per-source conservation) is written, because retaining every per-slot report
of a 256-slot boundary trace is not required to verify the result.  Standard
library only; run with the project src on PYTHONPATH.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from storage_dispatch.provenance import TraceSession  # noqa: E402
from tests.test_provenance_fullscale import (  # noqa: E402
    build_full_scale_document, N_BUSES, N_DEVICES, N_SOURCES, N_LINKS,
    N_SLOTS)


def main(argv):
    document = build_full_scale_document()
    raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
    if len(argv) > 1:
        Path(argv[1]).write_bytes(raw)

    session = TraceSession(document, retain_slots=False)
    slot_count = 0
    for frame in session._frames:
        slot_report = session.feed_validated(frame)
        # Exact cross-check before the report is released: the per-device
        # inventory sums equal the physical SOC on every slot boundary.
        from fractions import Fraction
        for device_report in slot_report["devices"]:
            total = sum((Fraction(e["energy"])
                         for e in device_report["after_origins"]),
                        Fraction(0))
            assert total == Fraction(device_report["after"])
        slot_count += 1
    report = session.finish()

    if len(argv) > 2:
        Path(argv[2]).write_text(
            json.dumps({"schema_version": report["schema_version"],
                        "buses": report["buses"],
                        "devices": report["devices"],
                        "source_summary": report["source_summary"]},
                       sort_keys=True, ensure_ascii=False, indent=2))
    residuals = {row["source_id"]: row["conservation_residual"]
                 for row in report["source_summary"]}
    assert set(residuals.values()) == {"0"}
    print("document: %d buses, %d devices, %d sources, %d links/frame, "
          "%d slots, %d bytes"
          % (N_BUSES, N_DEVICES, N_SOURCES, N_LINKS, N_SLOTS, len(raw)))
    print("streamed %d slots; all %d per-source conservation residuals are 0"
          % (slot_count, len(residuals)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
