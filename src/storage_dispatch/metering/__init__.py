"""Related-meter and joint ticket audit for storage operations.

Public, independently callable API (no CLI, no reservation book involved):

* :func:`run_ingest` / :func:`run_audit` -- end-to-end orchestration;
* :class:`~storage_dispatch.metering.schema.MeteringError`, strict validators;
* :class:`~storage_dispatch.metering.domain.MeteringDomain` -- the measurement
  feasible polyhedron, feasibility, full-slot witnesses and minimal reading
  conflicts;
* :class:`~storage_dispatch.metering.billing.BillingSolver` -- exact invoice
  extrema and the joint possible/impossible conclusion.

The reservation planner is never used as a metering fact; only the shared
physics contract and the generic exact LP engine are reused.
"""
from .schema import (MeteringError, parse_strict_json, validate_ingest,
                     validate_invoices, canonical_rational)
from .store import ingest, open_snapshot, load_dataset, StoreError
from .domain import (MeteringDomain, MeteringInfeasible, witness_to_jsonable)
from .billing import BillingSolver, InvoiceInfeasible
from .audit import run_ingest, run_audit

__all__ = [
    "MeteringError", "StoreError", "MeteringInfeasible", "InvoiceInfeasible",
    "parse_strict_json", "validate_ingest", "validate_invoices",
    "canonical_rational", "ingest", "open_snapshot", "load_dataset",
    "MeteringDomain", "BillingSolver", "witness_to_jsonable",
    "run_ingest", "run_audit",
]
