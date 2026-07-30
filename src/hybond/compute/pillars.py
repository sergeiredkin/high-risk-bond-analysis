"""Six-pillar credit metrics from standard РСБУ line codes. Pure functions, no
LLM, no I/O. This is Layer 1: the pillar SCORES are computed here and are
auditable; the hy-credit-scorecard skill only writes narrative around them.

The ГИР БО breakthrough is what puts this in deterministic Python: financials
arrive as {code: value} keyed by Минфин 66н codes, so the pillars are a lookup +
arithmetic, not an OCR/interpretation problem.

Standard codes used (РСБУ, приказ Минфина 66н):
  Balance:
    1150 основные средства         1170 финансовые вложения (внеоб.)
    1210 запасы                    1230 дебиторская задолженность
    1240 фин. вложения (обор.)     1250 денежные средства
    1200 итого оборотные активы    1300 итого капитал и резервы
    1400 итого долгосрочные обяз.  1410 долгосрочные заёмные средства
    1500 итого краткосрочные обяз. 1510 краткосрочные заёмные средства
    1520 кредиторская задолж.      1600 баланс (актив)
  Financial results:
    2110 выручка                   2120 себестоимость
    2200 прибыль (убыток) от продаж
    2330 проценты к уплате         2400 чистая прибыль (убыток)
  Cash flow:
    4100 сальдо ден. потоков от текущих операций (operating cash flow proxy)

All amounts are thousands of RUB. Ratios are unitless; where a denominator is
zero or a needed code is missing, the metric is None (never a fabricated value).
"""
from __future__ import annotations

from dataclasses import dataclass


def _g(m: dict[int, float], code: int) -> float | None:
    v = m.get(code)
    return float(v) if v is not None else None


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den in (None, 0):
        return None
    return num / den


@dataclass
class PillarInputs:
    """Raw quantities pulled from the standard statements, for transparency —
    the factlist/skill can show these alongside the derived ratios."""
    revenue: float | None
    ebit: float | None                 # 2200 прибыль от продаж (EBIT proxy)
    interest_expense: float | None     # 2330
    net_income: float | None           # 2400
    operating_cf: float | None         # 4100
    cash: float | None                 # 1250 + 1240
    current_assets: float | None       # 1200
    short_term_liab: float | None      # 1500
    short_term_debt: float | None      # 1510
    long_term_debt: float | None       # 1410
    total_debt: float | None           # 1410 + 1510
    equity: float | None               # 1300
    total_assets: float | None         # 1600


@dataclass
class Pillars:
    """The six pillars as computed metrics + a 0-100 sub-score each. Scores are
    deterministic mappings from the ratios (documented thresholds), so a human
    can audit every number. Direction: higher score = safer."""
    inputs: PillarInputs
    # leverage
    debt_to_ebit: float | None
    interest_coverage: float | None
    debt_to_assets: float | None
    # liquidity
    current_ratio: float | None
    cash_to_short_term_debt: float | None
    # refinancing
    short_term_debt_share: float | None       # ST debt / total debt
    # operations
    net_margin: float | None
    ocf_to_debt: float | None
    # sub-scores (0-100)
    score_leverage: int | None = None
    score_liquidity: int | None = None
    score_refinancing: int | None = None
    score_operations: int | None = None
    # governance & market pillars are NOT computed from financials — they come
    # from the event stream (Slice 3) and market data (Slice 1). Left None here
    # so the scorecard skill sources them from the factlist, not from РСБУ.
    coverage_note: str = ""

    def to_json(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "inputs"}
        d["inputs"] = self.inputs.__dict__
        return d


def extract_inputs(balance: dict[int, float], fin: dict[int, float],
                   cf: dict[int, float]) -> PillarInputs:
    lt_debt = _g(balance, 1410)
    st_debt = _g(balance, 1510)
    total_debt = None
    if lt_debt is not None or st_debt is not None:
        total_debt = (lt_debt or 0.0) + (st_debt or 0.0)
    cash = None
    c1250, c1240 = _g(balance, 1250), _g(balance, 1240)
    if c1250 is not None or c1240 is not None:
        cash = (c1250 or 0.0) + (c1240 or 0.0)
    return PillarInputs(
        revenue=_g(fin, 2110),
        ebit=_g(fin, 2200),
        interest_expense=_g(fin, 2330),
        net_income=_g(fin, 2400),
        operating_cf=_g(cf, 4100),
        cash=cash,
        current_assets=_g(balance, 1200),
        short_term_liab=_g(balance, 1500),
        short_term_debt=st_debt,
        long_term_debt=lt_debt,
        total_debt=total_debt,
        equity=_g(balance, 1300),
        total_assets=_g(balance, 1600),
    )


# --- scoring thresholds (documented, tunable) ------------------------------
# Each maps a ratio to 0-100. Conservative HY-oriented bands; tune with data.

def _score_bands(value: float | None, bands: list[tuple[float, int]],
                 higher_is_better: bool) -> int | None:
    """bands: sorted list of (threshold, score) from best to worst. For
    higher_is_better, value >= threshold earns the score."""
    if value is None:
        return None
    for thr, sc in bands:
        if (higher_is_better and value >= thr) or (not higher_is_better and value <= thr):
            return sc
    return bands[-1][1]


def compute_pillars(balance, fin, cf, balance_prev=None) -> Pillars:
    inp = extract_inputs(balance, fin, cf)

    debt_to_ebit = _ratio(inp.total_debt, inp.ebit)
    interest_coverage = _ratio(inp.ebit, inp.interest_expense)
    debt_to_assets = _ratio(inp.total_debt, inp.total_assets)
    current_ratio = _ratio(inp.current_assets, inp.short_term_liab)
    cash_to_st_debt = _ratio(inp.cash, inp.short_term_debt)
    st_debt_share = _ratio(inp.short_term_debt, inp.total_debt)
    net_margin = _ratio(inp.net_income, inp.revenue)
    ocf_to_debt = _ratio(inp.operating_cf, inp.total_debt)

    # leverage: lower debt/EBIT and higher coverage are safer
    s_lev = None
    if debt_to_ebit is not None:
        s_lev = _score_bands(debt_to_ebit,
                             [(2, 100), (3, 80), (4, 60), (5, 40), (7, 20)],
                             higher_is_better=False)
    # liquidity: current ratio + cash coverage of ST debt
    s_liq = None
    if current_ratio is not None:
        s_liq = _score_bands(current_ratio,
                            [(2.0, 100), (1.5, 80), (1.2, 60), (1.0, 40), (0.8, 20)],
                            higher_is_better=True)
    # refinancing: lower short-term-debt share is safer (less imminent rollover)
    s_ref = None
    if st_debt_share is not None:
        s_ref = _score_bands(st_debt_share,
                            [(0.2, 100), (0.35, 80), (0.5, 60), (0.7, 40), (0.9, 20)],
                            higher_is_better=False)
    # operations: net margin
    s_ops = None
    if net_margin is not None:
        s_ops = _score_bands(net_margin,
                           [(0.15, 100), (0.08, 80), (0.03, 60), (0.0, 40), (-0.05, 20)],
                           higher_is_better=True)

    note = ""
    if inp.total_debt is None:
        note = "no debt lines (1410/1510) found — leverage/refinancing unreliable"

    return Pillars(
        inputs=inp,
        debt_to_ebit=debt_to_ebit, interest_coverage=interest_coverage,
        debt_to_assets=debt_to_assets, current_ratio=current_ratio,
        cash_to_short_term_debt=cash_to_st_debt, short_term_debt_share=st_debt_share,
        net_margin=net_margin, ocf_to_debt=ocf_to_debt,
        score_leverage=s_lev, score_liquidity=s_liq, score_refinancing=s_ref,
        score_operations=s_ops, coverage_note=note,
    )


PILLAR_SCORE_KEYS = ["score_leverage", "score_liquidity", "score_refinancing", "score_operations"]


def pillars_delta(current: dict, previous: dict | None) -> dict:
    """Per-pillar current-vs-previous-snapshot delta -- 'improving' (score
    up, higher is always safer here)/'degrading'/'flat'. Deliberately NOT a
    z-score model like diff/engine.py's stat detector for G-spread: that
    needs ~8+ snapshots of history to characterise normal noise, and
    pillars_snapshot doesn't have that yet for any issuer (see
    db.pillars_history). This is a plain two-point comparison, ready to
    accumulate into something more statistical once real weekly history
    exists -- don't oversell it as a trend in the meantime."""
    if not previous:
        return {k: {"delta": None, "direction": None} for k in PILLAR_SCORE_KEYS}
    out = {}
    for k in PILLAR_SCORE_KEYS:
        cur, prev = current.get(k), previous.get(k)
        if cur is None or prev is None:
            out[k] = {"delta": None, "direction": None}
            continue
        d = cur - prev
        out[k] = {"delta": d,
                   "direction": "improving" if d > 0 else "degrading" if d < 0 else "flat"}
    return out
