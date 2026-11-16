"""Strict request parsing and domain validation for the reservation book.

Every public request is a raw UTF-8 JSON byte string.  Parsing rejects
duplicate object keys, non-finite JSON constants and unknown fields at every
nesting level; numeric inputs must be genuine ``int`` values (booleans are
not accepted).  All validation is exact and dependency free.
"""
import json
import re
from fractions import Fraction

ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
MAX_SLOTS = 192
MAX_CONTRACTS = 80
LIMIT_MAX = 10 ** 9
ENERGY_MAX = 10 ** 9
EFF_PART_MAX = 1_000_000


class RequestError(ValueError):
    """A request failed schema or domain validation."""


class Conflict(RequestError):
    """A request id was already committed with different bytes."""


def parse_json(raw):
    """Parse *raw* (bytes) strictly and return the decoded Python object."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, (bytes, bytearray)):
        raise RequestError("request must be UTF-8 JSON bytes")

    def reject_duplicates(pairs):
        seen = set()
        out = {}
        for key, value in pairs:
            if key in seen:
                raise RequestError("duplicate JSON key: " + str(key))
            seen.add(key)
            out[key] = value
        return out

    def reject_constant(value):
        raise RequestError("non-finite JSON constant: " + value)

    try:
        return json.loads(raw, object_pairs_hook=reject_duplicates,
                          parse_constant=reject_constant)
    except RequestError:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise RequestError("invalid JSON: " + str(exc))


def _exact(obj, keys, what):
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise RequestError("%s must be an object with exactly fields: %s"
                           % (what, ", ".join(keys)))


def _int(obj, key, lo, hi, what):
    value = obj.get(key)
    if type(value) is not int or not lo <= value <= hi:
        raise RequestError("%s.%s must be an integer in [%d, %d]"
                           % (what, key, lo, hi))
    return value


def _id(obj, key, what):
    value = obj.get(key)
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise RequestError("%s.%s must be 1..64 ASCII letters, digits, '_' or '-'"
                           % (what, key))
    return value


def _efficiency(obj, key, what):
    value = obj.get(key)
    if not isinstance(value, dict) or set(value) != {"numerator", "denominator"}:
        raise RequestError("%s.%s must be {numerator, denominator}" % (what, key))
    num = _int(value, "numerator", 1, EFF_PART_MAX, what + "." + key)
    den = _int(value, "denominator", 1, EFF_PART_MAX, what + "." + key)
    if num > den:
        raise RequestError("%s.%s must be <= 1" % (what, key))
    return Fraction(num, den)


def validate_init(data):
    _exact(data, ["capacity", "initial", "charge_efficiency",
                  "discharge_efficiency", "terminal_min", "slots"], "init")
    what = "init"
    capacity = _int(data, "capacity", 1, LIMIT_MAX, what)
    initial = _int(data, "initial", 0, capacity, what)
    eta_c = _efficiency(data, "charge_efficiency", what)
    eta_d = _efficiency(data, "discharge_efficiency", what)
    terminal_min = _int(data, "terminal_min", 0, capacity, what)
    slots_raw = data["slots"]
    if not isinstance(slots_raw, list) or not 1 <= len(slots_raw) <= MAX_SLOTS:
        raise RequestError("init.slots must be 1..%d slot objects" % MAX_SLOTS)
    slots = []
    for idx, slot in enumerate(slots_raw):
        label = "init.slots[%d]" % idx
        _exact(slot, ["charge_limit", "discharge_limit", "price"], label)
        cl = _int(slot, "charge_limit", 0, LIMIT_MAX, label)
        dl = _int(slot, "discharge_limit", 0, LIMIT_MAX, label)
        price = _int(slot, "price", 0, LIMIT_MAX, label)
        if cl > 0 and dl > 0:
            raise RequestError(label + ": charge_limit and discharge_limit "
                                       "cannot both be positive")
        slots.append({"charge_limit": cl, "discharge_limit": dl, "price": price})
    return {
        "capacity": capacity,
        "initial": initial,
        "eta_c": eta_c,
        "eta_d": eta_d,
        "terminal_min": terminal_min,
        "slots": slots,
    }


def validate_add(data, cursor, slot_count, existing_contracts):
    _exact(data, ["request_id", "contract_id", "start", "end", "energy"], "add")
    request_id = _id(data, "request_id", "add")
    contract_id = _id(data, "contract_id", "add")
    start = _int(data, "start", cursor, slot_count - 1, "add")
    end = _int(data, "end", start + 1, slot_count, "add")
    energy = _int(data, "energy", 1, ENERGY_MAX, "add")
    if contract_id in existing_contracts:
        raise RequestError("contract_id is never reused: " + contract_id)
    if len(existing_contracts) >= MAX_CONTRACTS:
        raise RequestError("the book holds at most %d contracts (including "
                           "cancelled/completed)" % MAX_CONTRACTS)
    return {
        "request_id": request_id,
        "contract_id": contract_id,
        "start": start,
        "end": end,
        "energy": energy,
    }


def validate_cancel(data):
    _exact(data, ["request_id", "contract_id"], "cancel")
    return {
        "request_id": _id(data, "request_id", "cancel"),
        "contract_id": _id(data, "contract_id", "cancel"),
    }


def validate_advance(data, cursor, slot_count):
    _exact(data, ["request_id", "to_slot"], "advance")
    request_id = _id(data, "request_id", "advance")
    to_slot = _int(data, "to_slot", cursor + 1, slot_count, "advance")
    return {"request_id": request_id, "to_slot": to_slot}
