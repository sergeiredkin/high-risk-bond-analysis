"""Offline fixtures shaped like real MOEX/CBR data so the compute + calendar
layers are fully testable without network."""
from datetime import date

# A synthetic ВДО-like bond: 1000 face, ~18% coupon paid quarterly, ~2y left,
# trading at a discount (92% of face).
SETTLEMENT = date(2026, 7, 25)

COUPONS = [
    (date(2026, 10, 25), 45.0),
    (date(2027, 1, 25), 45.0),
    (date(2027, 4, 25), 45.0),
    (date(2027, 7, 25), 45.0),
    (date(2027, 10, 25), 45.0),
    (date(2028, 1, 25), 45.0),
    (date(2028, 4, 25), 45.0),
    (date(2028, 7, 25), 45.0),
]
# bullet redemption at maturity
AMORTS = [(date(2028, 7, 25), 1000.0)]

LAST_PCT = 92.0  # clean price, % of face

# CBR-style zero curve (tenor_years, decimal rate)
CURVE = [
    (0.25, 0.150), (0.5, 0.148), (0.75, 0.146), (1.0, 0.144),
    (2.0, 0.140), (3.0, 0.138), (5.0, 0.136), (10.0, 0.135),
]

# Multi-issuer flows for the maturity-wall test
FLOWS_BY_ISSUER = {
    "7701234567": [
        (date(2026, 8, 15), 30.0, "coupon"),
        (date(2026, 11, 15), 30.0, "coupon"),
        (date(2027, 2, 15), 1000.0, "redemption"),
    ],
    "7707654321": [
        (date(2026, 9, 1), 25.0, "coupon"),
        (date(2026, 9, 1), 500.0, "amortization"),
        (date(2027, 3, 1), 25.0, "coupon"),
        (date(2027, 3, 1), 500.0, "amortization"),
    ],
}
