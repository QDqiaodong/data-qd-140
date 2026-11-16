from fractions import Fraction

from .physics import (is_charge, charge_headroom, discharge_headroom,
                      soc_delta, slot_losses)


def simulate(capacity, initial, max_charge, max_discharge, eta_c, eta_d,
             requests):
    """Deterministic v1 saturated replay.

    The direction, grid-side limits, efficiency and SOC conversions all come
    from :mod:`storage_dispatch.physics`, the same contract the metering
    domain uses.  The saturation policy (accept as much as possible) and the
    output shape are the original v1 behaviour and must not change.
    """
    soc = Fraction(initial)
    total_in = total_out = losses = unmet = Fraction()
    steps = []
    for i, r in enumerate(requests):
        before = soc
        grid_in = grid_out = loss = Fraction()
        if is_charge(r):
            grid_in = charge_headroom(soc, capacity, eta_c, max_charge, r)
            soc += soc_delta(True, eta_c, eta_d, grid_in)
            loss = slot_losses(True, eta_c, eta_d, grid_in)
            missing = r - grid_in
        else:
            grid_out = discharge_headroom(soc, eta_d, max_discharge, r)
            soc += soc_delta(False, eta_c, eta_d, grid_out)
            loss = slot_losses(False, eta_c, eta_d, grid_out)
            missing = -r - grid_out
        total_in += grid_in
        total_out += grid_out
        losses += loss
        unmet += missing
        steps.append({'slot': i, 'request': r, 'before': str(before),
                      'after': str(soc), 'grid_in': str(grid_in),
                      'grid_out': str(grid_out), 'loss': str(loss),
                      'unmet': str(missing)})
    return {'steps': steps, 'final': str(soc), 'grid_in': str(total_in),
            'grid_out': str(total_out), 'loss': str(losses),
            'unmet': str(unmet),
            'conservation_residual': str(
                Fraction(initial) + total_in - total_out - losses - soc)}
