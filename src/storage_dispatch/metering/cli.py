"""CLI wiring for the metering ingest and joint invoice audit."""
import json
import sys
from pathlib import Path

from ..common import save
from ..metering import run_ingest, run_audit, MeteringError, StoreError
from ..metering.domain import MeteringInfeasible
from ..metering.billing import InvoiceInfeasible


def _read_bytes(path):
    if str(path) == "-":
        return sys.stdin.buffer.read()
    return Path(path).read_bytes()


def _resolve(path):
    return Path(path).resolve()


def run_metering(args):
    if args.meter_action == "ingest":
        raw = _read_bytes(args.source)
        receipt = run_ingest(args.db, raw)
        print(json.dumps(receipt, sort_keys=True, ensure_ascii=False,
                         allow_nan=False))
        return 0

    # audit: never publish over an input and never through a symlink.
    output = Path(args.output)
    if output.is_symlink():
        raise MeteringError("output cannot be a symlink")
    if str(args.invoices) != "-":
        if _resolve(args.invoices) == output.resolve():
            raise MeteringError("input and output must differ")
    if _resolve(args.db) == output.resolve():
        raise MeteringError("output must differ from the database path")
    invoice_bytes = _read_bytes(args.invoices)
    report = run_audit(args.db, args.dataset, invoice_bytes)
    save(output, report)
    print(json.dumps({"status": report["status"],
                      "output": str(output)}, sort_keys=True,
                     ensure_ascii=False, allow_nan=False))
    return 0


def add_metering_parser(sub):
    meter = sub.add_parser("metering",
                           help="related-meter and joint invoice audit")
    msub = meter.add_subparsers(dest="meter_action", required=True)

    ingest_p = msub.add_parser("ingest", help="store meter observations")
    ingest_p.add_argument("--db", type=Path, required=True)
    ingest_p.add_argument("--source", type=Path, required=True)

    audit_p = msub.add_parser("audit", help="audit invoices against a dataset")
    audit_p.add_argument("--db", type=Path, required=True)
    audit_p.add_argument("--dataset", required=True)
    audit_p.add_argument("--invoices", type=Path, required=True)
    audit_p.add_argument("--output", type=Path, required=True)


METERING_FAILURES = (MeteringError, StoreError, MeteringInfeasible,
                     InvoiceInfeasible, ValueError, TypeError, KeyError,
                     OSError, OverflowError)
