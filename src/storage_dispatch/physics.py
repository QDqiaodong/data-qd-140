"""Shared storage physics contract.

This module is the single source of truth for the direction convention, the
grid-side limits, the efficiency conversions and the SOC bookkeeping used by:

* the v1 deterministic request replay (:mod:`storage_dispatch.dispatch`), and
* the metering feasible domain (:mod:`storage_dispatch.metering.domain`).

No efficiency formula is duplicated anywhere else; every caller goes through
:func:`efficiency` and the ``*_headroom`` / :func:`soc_delta` /
:func:`slot_losses` helpers.  All arithmetic is exact :class:`Fraction`.

Conventions (unchanged from v1):

* request ``r >= 0`` (including zero) is the charge direction; ``r < 0`` is the
  discharge direction, and a slot only ever moves in that one direction;
* charge: grid energy ``e`` raises SOC by ``e * eta_c`` and loses
  ``e * (1 - eta_c)`` on the grid side;
* discharge: grid energy ``e`` lowers SOC by ``e / eta_d`` and loses
  ``e * (1 / eta_d - 1)``;
* the grid-side accepted energy is bounded by the request magnitude, the
  relevant device limit and the current SOC headroom.
"""
from fractions import Fraction

EFF_PART_MAX = 1_000_000
LIMIT_MAX = 10 ** 9


def efficiency(numerator, denominator):
    """Validate an efficiency ratio and return it as an exact Fraction.

    Both parts are genuine integers in ``1..10**6`` and the ratio is in
    ``(0, 1]``.
    """
    if type(numerator) is not int or type(denominator) is not int:
        raise ValueError('efficiency parts must be integers')
    if not (1 <= numerator <= EFF_PART_MAX and
            1 <= denominator <= EFF_PART_MAX):
        raise ValueError('efficiency parts must be in 1..%d' % EFF_PART_MAX)
    if numerator > denominator:
        raise ValueError('efficiency must be <=1')
    return Fraction(numerator, denominator)


def is_charge(request):
    """A non-negative request (zero included) is the charge direction."""
    return request >= 0


def request_magnitude(request):
    return Fraction(request) if request >= 0 else Fraction(-request)


def charge_headroom(soc, capacity, eta_c, max_charge, request):
    """Grid-side charge energy accepted in a saturated v1 replay slot."""
    return min(Fraction(request), Fraction(max_charge),
               (Fraction(capacity) - soc) / eta_c)


def discharge_headroom(soc, eta_d, max_discharge, request):
    """Grid-side discharge energy accepted in a saturated v1 replay slot."""
    return min(Fraction(-request), Fraction(max_discharge), soc * eta_d)


def soc_delta(charge, eta_c, eta_d, energy):
    """SOC change produced by grid-side *energy* in the given direction."""
    return energy * eta_c if charge else -energy / eta_d


def slot_losses(charge, eta_c, eta_d, energy):
    """Grid-side conversion loss for the accepted energy."""
    if charge:
        return energy * (Fraction(1) - eta_c)
    return energy * (Fraction(1) / eta_d - Fraction(1))


def step_request(soc, capacity, eta_c, eta_d, max_charge, max_discharge,
                 request):
    """Advance one saturated v1 slot and return every physical quantity.

    This is the single reusable per-slot physical step shared by the v1
    deterministic replay (:func:`storage_dispatch.dispatch.simulate`) and the
    provenance :class:`~storage_dispatch.provenance.engine.TraceSession`.
    There is deliberately no second acceptance/loss formula: both callers
    saturate the same request, device-limit and SOC headroom rules.

    Return keys: ``charge`` (direction flag), ``grid_in``, ``grid_out``,
    ``loss``, ``unmet`` (all grid-side :class:`Fraction`) and ``after`` (the
    new SOC).  A zero request takes the charge direction and accepts nothing.
    """
    soc = Fraction(soc)
    if is_charge(request):
        grid_in = charge_headroom(soc, capacity, eta_c, max_charge, request)
        after = soc + soc_delta(True, eta_c, eta_d, grid_in)
        loss = slot_losses(True, eta_c, eta_d, grid_in)
        return {'charge': True, 'grid_in': grid_in,
                'grid_out': Fraction(0), 'loss': loss,
                'unmet': Fraction(request) - grid_in, 'after': after}
    grid_out = discharge_headroom(soc, eta_d, max_discharge, request)
    after = soc + soc_delta(False, eta_c, eta_d, grid_out)
    loss = slot_losses(False, eta_c, eta_d, grid_out)
    return {'charge': False, 'grid_in': Fraction(0),
            'grid_out': grid_out, 'loss': loss,
            'unmet': Fraction(-request) - grid_out, 'after': after}
