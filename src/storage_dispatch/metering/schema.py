"""Strict schema validation for the metering ingest and invoice audit.

Every input is parsed from its original UTF-8 JSON bytes so that illegal UTF-8,
duplicate object keys, ``NaN``/``Infinity`` constants and unknown fields fail
the whole operation.  Booleans are never accepted where integers are required
and floats are never accepted where exact rationals are required.

Canonical rational strings are exactly the strings produced by
``str(Fraction)`` whose reduced denominator is at most ``10**6``: ``"0"``,
``"3"``, ``"25/8"`` -- never ``"1.5"``, ``"1e2"``, ``"3/1"`` or ``" 1 "``.
"""
import json
import re

from fractions import Fraction

from ..common import number
from ..physics import efficiency

ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
CHANNELS = ("import", "export", "loss")
MAX_REQUEST_SLOTS = 24
MAX_READINGS = 64
MAX_INVOICES = 4
RATIONAL_MAX = 10 ** 12
RATIONAL_DEN_MAX = 10 ** 6
PRICE_MAX = 10 ** 9


class MeteringError(ValueError):
    """An input failed schema or domain validation."""


def parse_strict_json(raw):
    """Parse exact UTF-8 JSON *raw* bytes with the strictest settings."""
    if not isinstance(raw, (bytes, bytearray)):
        raise MeteringError("input must be JSON bytes")
    raw = bytes(raw)

    def reject_duplicates(pairs):
        seen = set()
        for key, _ in pairs:
            if key in seen:
                raise MeteringError("duplicate JSON key: " + str(key))
            seen.add(key)
        return dict(pairs)

    def reject_constant(value):
        raise MeteringError("non-finite JSON constant: " + value)

    try:
        return json.loads(raw, object_pairs_hook=reject_duplicates,
                          parse_constant=reject_constant)
    except MeteringError:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise MeteringError("invalid JSON: " + str(exc))


def _exact(obj, keys, what):
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise MeteringError("%s must be an object with exactly fields: %s"
                            % (what, ", ".join(keys)))


def _id(value, what):
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise MeteringError("%s must be 1..64 ASCII letters, digits, '_' or '-'"
                            % what)
    return value


def canonical_rational(value, what, maximum=None, allow_zero=True):
    """Return the exact Fraction behind a canonical rational string.

    The string must equal ``str(Fraction(value))`` (so it is reduced and has no
    sign/whitespace/exponent variants), the denominator may not exceed
    ``10**6`` and the value must be non-negative.
    """
    if not isinstance(value, str):
        raise MeteringError("%s must be a canonical rational string" % what)
    try:
        parsed = Fraction(value)
    except (ValueError, ZeroDivisionError):
        raise MeteringError("%s is not a canonical rational string" % what)
    if str(parsed) != value or parsed.denominator > RATIONAL_DEN_MAX:
        raise MeteringError("%s is not a canonical rational string" % what)
    if parsed < 0 or (not allow_zero and parsed == 0):
        raise MeteringError("%s must be non-negative" % what)
    if maximum is not None and parsed > maximum:
        raise MeteringError("%s exceeds %s" % (what, maximum))
    return parsed


def _device(obj, what):
    """Validate the v1 source device object with its original field ranges."""
    _exact(obj, ["capacity", "initial", "max_charge", "max_discharge",
                 "charge_efficiency", "discharge_efficiency", "requests"],
           what)
    capacity = number(obj["capacity"], 1, 10**9)
    initial = number(obj["initial"], 0, capacity)
    max_charge = number(obj["max_charge"], 0, 10**9)
    max_discharge = number(obj["max_discharge"], 0, 10**9)
    for key in ("charge_efficiency", "discharge_efficiency"):
        eff = obj[key]
        if not isinstance(eff, dict) or set(eff) != {"numerator",
                                                     "denominator"}:
            raise MeteringError("%s.%s must be {numerator, denominator}"
                                % (what, key))
    eta_c = efficiency(
        number(obj["charge_efficiency"]["numerator"], 1, 10**6),
        number(obj["charge_efficiency"]["denominator"], 1, 10**6))
    eta_d = efficiency(
        number(obj["discharge_efficiency"]["numerator"], 1, 10**6),
        number(obj["discharge_efficiency"]["denominator"], 1, 10**6))
    requests_raw = obj["requests"]
    if not isinstance(requests_raw, list) or not (
            1 <= len(requests_raw) <= MAX_REQUEST_SLOTS):
        raise MeteringError("%s.requests must have 1..%d slots"
                            % (what, MAX_REQUEST_SLOTS))
    requests = [number(r, -10**9, 10**9) for r in requests_raw]
    return {
        "capacity": capacity,
        "initial": initial,
        "max_charge": max_charge,
        "max_discharge": max_discharge,
        "eta_c": eta_c,
        "eta_d": eta_d,
        "requests": requests,
    }


def validate_ingest(data):
    """Validate ``{device, readings}`` and return normalized structures."""
    try:
        return _validate_ingest(data)
    except MeteringError:
        raise
    except (ValueError, TypeError) as exc:
        raise MeteringError(str(exc))


def _validate_ingest(data):
    _exact(data, ["device", "readings"], "ingest payload")
    device = _device(data["device"], "device")
    slots = len(device["requests"])
    readings_raw = data["readings"]
    if not isinstance(readings_raw, list) or len(readings_raw) > MAX_READINGS:
        raise MeteringError("readings must be an array of at most %d items"
                            % MAX_READINGS)
    readings = []
    seen = set()
    for i, item in enumerate(readings_raw):
        what = "readings[%d]" % i
        _exact(item, ["reading_id", "channel", "start", "end", "lower",
                      "upper"], what)
        reading_id = _id(item["reading_id"], what + ".reading_id")
        if reading_id in seen:
            raise MeteringError("duplicate reading_id: " + reading_id)
        seen.add(reading_id)
        if item["channel"] not in CHANNELS:
            raise MeteringError(what + ".channel must be import/export/loss")
        channel = item["channel"]
        start = number(item["start"], 0, slots)
        end = number(item["end"], 0, slots)
        if not start < end:
            raise MeteringError(what + " requires 0 <= start < end <= slots")
        lower = canonical_rational(item["lower"], what + ".lower",
                                   RATIONAL_MAX)
        upper = canonical_rational(item["upper"], what + ".upper",
                                   RATIONAL_MAX)
        if lower > upper:
            raise MeteringError(what + " requires lower <= upper")
        readings.append({
            "reading_id": reading_id,
            "channel": channel,
            "start": start,
            "end": end,
            "lower": lower,
            "upper": upper,
        })
    return device, readings, slots


def validate_invoices(data, slots):
    """Validate the invoice array (at most four tickets)."""
    try:
        return _validate_invoices(data, slots)
    except MeteringError:
        raise
    except (ValueError, TypeError) as exc:
        raise MeteringError(str(exc))


def _validate_invoices(data, slots):
    if not isinstance(data, list) or len(data) > MAX_INVOICES:
        raise MeteringError("invoices must be an array of at most %d items"
                            % MAX_INVOICES)
    invoices = []
    seen = set()
    for i, item in enumerate(data):
        what = "invoices[%d]" % i
        _exact(item, ["invoice_id", "start", "end", "prices", "window",
                      "demand_rate", "amount"], what)
        invoice_id = _id(item["invoice_id"], what + ".invoice_id")
        if invoice_id in seen:
            raise MeteringError("duplicate invoice_id: " + invoice_id)
        seen.add(invoice_id)
        start = number(item["start"], 0, slots)
        end = number(item["end"], 0, slots)
        if not start < end:
            raise MeteringError(what + " requires 0 <= start < end <= slots")
        prices = item["prices"]
        if not isinstance(prices, list) or len(prices) != end - start:
            raise MeteringError(what + ".prices length must equal end-start")
        prices = [number(p, 0, PRICE_MAX) for p in prices]
        window = number(item["window"], 1, end - start)
        demand_rate = number(item["demand_rate"], 0, PRICE_MAX)
        amount = canonical_rational(item["amount"], what + ".amount")
        invoices.append({
            "invoice_id": invoice_id,
            "start": start,
            "end": end,
            "prices": prices,
            "window": window,
            "demand_rate": demand_rate,
            "amount": amount,
        })
    return invoices
