"""Noise pre-filter for the manual classification queue (`hybond queue` /
`hybond market-notes-queue`). Purely a QUEUE-DISPLAY filter -- it never
changes what gets scraped or stored; filing/market_note rows land in the DB
exactly as before regardless of what this says. See scrape_pipeline.py's
classify_queue_view()/market_note_queue_view() for where it's applied, and
db.skip_noise_filings() for the --skip-noise bulk-classify path.

Distinct from scripts/bulk_mark_routine.py's ROUTINE_RULES: that script
exact-matches specific (issuer_id, title) pairs it has sampled and confirmed
generalize (or deliberately doesn't, e.g. Брусника vs Селигдар on the same
shared title -- see its own docstring). This module instead does a cruder,
issuer-agnostic SUBSTRING match against a fixed keyword list, meant to catch
the broad, obviously-routine cases (coupon payments, AGM notices, listing
admissions) without per-issuer sampling. The two are complementary, not
redundant.
"""
from __future__ import annotations

NOISE_PATTERNS = [
    "выплата купона",
    "coupon payment made on schedule",
    "routine coupon",
    "плановая выплата",
    "годовое общее собрание",  # standard AGM, no material decisions
    "annual general meeting",
    "новый выпуск зарегистрирован",  # routine listing step
    "admitted to exchange trading",
    "not yet placed",
    # Added after auditing the real 1864-filing queue: these are the ACTUAL
    # AZIPI titles for "coupon paid"/"coupon accrued" (615+117 = 732 of the
    # 1864, ~39%) -- the literal "выплата купона"/"плановая выплата" phrases
    # above never appear verbatim in real filings.
    "выплаченные доходы",
    "начисленные доходы",
    "сведения о выплаченных",
    "сведения о начисленных",
]

PRIORITY_PATTERNS = [
    "дефолт", "default",
    "оферта", "offer_put", "put_call",
    "рейтинг", "rating",
    "реструктуризация", "restructur",
    "судебное", "court", "арбитраж",
    "задержка", "delay", "не раскрыл",
    "техдефолт", "technical default",
    "просрочка", "overdue",
    # Added after auditing the real queue: these are the real AZIPI titles
    # for a covenant/default-adjacent event (166 filings) and a put-right
    # trigger (69 filings) -- neither contains "дефолт" or "оферта"
    # literally, so both were silently landing in "normal" instead of
    # "priority" before this.
    "неисполнение обязательств",
    "права требовать досрочного погашения",
]


def queue_bucket(title: str | None) -> str:
    """One of 'priority' / 'noise' / 'normal', case-insensitive substring
    match. Priority always overrides noise -- a title matching both lists
    (e.g. a coupon notice that also mentions a rating action) is never
    silently buried as noise."""
    if not title:
        return "normal"
    t = title.lower()
    if any(p in t for p in PRIORITY_PATTERNS):
        return "priority"
    if any(p in t for p in NOISE_PATTERNS):
        return "noise"
    return "normal"


def is_likely_noise(title: str) -> bool:
    """True if `title` matches a noise pattern and no priority pattern.
    Thin wrapper over queue_bucket() -- the exact signature this task's
    Part 2 asks for."""
    return queue_bucket(title) == "noise"
