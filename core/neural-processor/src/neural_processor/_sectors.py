"""Fama-French 12 industries from 4-digit SIC codes; anything unlisted is "Other"."""

from __future__ import annotations

_FF12 = {
    "Consumer non-durables": [(100, 999), (2000, 2399), (2700, 2749), (2770, 2799), (3100, 3199),
                              (3940, 3989)],
    "Consumer durables": [(2500, 2519), (2590, 2599), (3630, 3659), (3710, 3711), (3714, 3714),
                          (3716, 3716), (3750, 3751), (3792, 3792), (3900, 3939), (3990, 3999)],
    "Manufacturing": [(2520, 2589), (2600, 2699), (2750, 2769), (3000, 3099), (3200, 3569),
                      (3580, 3629), (3700, 3709), (3712, 3713), (3715, 3715), (3717, 3749),
                      (3752, 3791), (3793, 3799), (3830, 3839), (3860, 3899)],
    "Energy": [(1200, 1399), (2900, 2999)],
    "Chemicals": [(2800, 2829), (2840, 2899)],
    "Business equipment": [(3570, 3579), (3660, 3692), (3694, 3699), (3810, 3829), (7370, 7379)],
    "Telecom": [(4800, 4899)],
    "Utilities": [(4900, 4949)],
    "Shops": [(5000, 5999), (7200, 7299), (7600, 7699)],
    "Healthcare": [(2830, 2839), (3693, 3693), (3840, 3859), (8000, 8099)],
    "Finance": [(6000, 6999)],
}
SECTORS = [*_FF12, "Other"]


def sic_sector(sic: str | int | None) -> str | None:
    """Fama-French 12 sector of a SIC code ('3571' -> 'Business equipment'); None if unknown."""
    try:
        code = int(sic)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    for name, ranges in _FF12.items():
        if any(lo <= code <= hi for lo, hi in ranges):
            return name
    return "Other"
