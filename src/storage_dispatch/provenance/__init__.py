"""Multi-battery energy-source provenance tracing.

Public, independently callable API (no reservation book, meter store or
billing involved, and no dispatch optimization or unknown-meter inference):

* :func:`trace_provenance` -- batch entry point taking strict JSON bytes;
* :func:`trace_document` -- batch entry point for an already parsed document;
* :class:`TraceSession` -- slot-by-slot ``feed``/``finish`` tracing whose
  failed feeds never mutate device state;
* :func:`solve_slot` -- the exact same-slot bus mixing solver;
* strict validators and the :class:`ProvenanceError`, :class:`Unbalanced`,
  :class:`AmbiguousOrigin` failure types.

Only the Python standard library is used and every energy value is an exact
:class:`~fractions.Fraction`.  Exact rational components can legitimately
have more numerator digits than CPython's 3.11 default string-conversion
limit, so that safety cap is lifted for this package's exact arithmetic;
the 8 MB input cap still bounds every accepted document.
"""
import sys as _sys

if hasattr(_sys, "set_int_max_str_digits"):  # pragma: no cover - CPython 3.11+
    _sys.set_int_max_str_digits(0)

from .schema import (ProvenanceError, Unbalanced, AmbiguousOrigin,
                     parse_strict_json, validate_document, validate_frame,
                     canonical_rational)
from .mixing import solve_slot
from .engine import TraceSession, trace_document, trace_provenance

__all__ = [
    "ProvenanceError", "Unbalanced", "AmbiguousOrigin",
    "parse_strict_json", "validate_document", "validate_frame",
    "canonical_rational", "solve_slot", "TraceSession",
    "trace_document", "trace_provenance",
]
