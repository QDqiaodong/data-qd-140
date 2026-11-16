"""End-to-end metering ingest and invoice audit orchestration."""
import datetime as _dt
import json

from .schema import (parse_strict_json, validate_ingest, validate_invoices,
                     MeteringError)
from .store import ingest, open_snapshot, load_dataset, digest_of, StoreError
from .domain import MeteringDomain, witness_to_jsonable
from .billing import BillingSolver, invoice_result_jsonable


def _now():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_ingest(db_path, source_bytes):
    """Validate and atomically store a ``{device, readings}`` payload."""
    data = parse_strict_json(source_bytes)
    device, readings, slots = validate_ingest(data)
    sha, reused = ingest(db_path, source_bytes, device, readings, slots,
                         _now())
    return {
        "dataset_sha256": sha,
        "source_sha256": sha,
        "reused": reused,
        "slot_count": slots,
        "reading_count": len(readings),
        "stored_raw_bytes": len(source_bytes),
    }


def run_audit(db_path, dataset_sha, invoice_bytes, output_path=None):
    """Audit one stored dataset against an array of at most four invoices.

    The database is opened strictly read-only; the invoice bytes are never
    written anywhere.  Returns the full report structure; the CLI layer owns
    atomic publication.
    """
    invoice_data = parse_strict_json(invoice_bytes)
    conn, source_bytes = open_snapshot(db_path, dataset_sha)
    try:
        device, readings, slots = load_dataset(conn, dataset_sha)
    finally:
        conn.close()
    invoices = validate_invoices(invoice_data, slots)
    domain = MeteringDomain(device, readings, slots)

    report = {
        "dataset_sha256": dataset_sha,
        "source_sha256": digest_of(source_bytes),
        "invoice_source_sha256": digest_of(invoice_bytes),
        "slot_count": slots,
    }

    # The measurement domain is decided first.  If it is inconsistent there
    # are no invoice bounds and no invoice conflicts to report.
    conflict = domain.reading_conflict()
    if conflict is not None:
        report["status"] = "inconsistent"
        report["reading_conflict"] = {
            "reading_ids": conflict["reading_ids"],
            "removals": [{
                "reading_id": item["reading_id"],
                "witness": witness_to_jsonable(item["witness"]),
            } for item in conflict["removals"]],
        }
        return report

    measurement_witness = witness_to_jsonable(domain.witness())
    solver = BillingSolver(domain, invoices)

    invoice_reports = []
    for i, inv in enumerate(invoices):
        f_min, f_max, min_witness, max_witness = solver.invoice_bounds(i)
        invoice_reports.append(invoice_result_jsonable(
            inv["invoice_id"], inv["amount"], f_min, f_max,
            min_witness, max_witness))
    report["invoices"] = invoice_reports

    common = solver.joint_witness()
    if common is not None:
        report["status"] = "possible"
        report["joint_witness"] = witness_to_jsonable(common)
    else:
        report["status"] = "impossible"
        conflict = solver.joint_conflict()
        report["invoice_conflict"] = {
            "invoice_ids": conflict["invoice_ids"],
            "removals": [{
                "invoice_id": item["invoice_id"],
                "witness": witness_to_jsonable(item["witness"]),
            } for item in conflict["removals"]],
        }
    report["measurement_witness"] = measurement_witness
    return report
