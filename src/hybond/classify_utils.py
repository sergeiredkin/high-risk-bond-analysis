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


# MOEX Listing / CBR Press task (2026-08-17): source-specific pattern lists,
# added alongside (not replacing) the AZIPI lists above. Verified against
# live-fetched feed content before finalizing -- see each list's own note
# on what was actually confirmed vs. extrapolated from convention.
#
# Both real feeds use bureaucratic gerund/prepositional-case titles ("О
# приостановке торгов", "Об исключении ценных бумаг из Списка"), NOT the
# nominative/short-form phrasing a first guess would reach for ("остановка
# торгов", "исключен из списка") -- those literal forms do NOT appear as
# substrings of the real inflected titles (Russian case endings), so they
# would silently never fire. Patterns below were built from the actual
# fetched titles (255 MOEX items, 8 CBR items, both pulled and inspected
# 2026-08-17), not the initial guess.
MOEX_LISTING_PRIORITY_PATTERNS = [
    "дефолт",
    "приостановке торгов",             # real: "О приостановке торгов ценными бумагами"
    "исключении ценных бумаг из списка",  # real: "Об исключении ценных бумаг из Списка ценных бумаг..."
    "прекращении торгов",              # real: "...и о прекращении торгов ценными бумагами"
    "несостоявш",                      # root -- matches несостоявшимися/несостоявшейся (failed placement/program)
    "аннулировании регистрации",       # real: "...об аннулировании регистрации выпусков"
    "выявленном несоответствии",       # real: "О выявленном несоответствии ценных бумаг"
]

MOEX_LISTING_NOISE_PATTERNS = [
    "о начале торгов",                 # routine daily "trading starts" notice -- by far the most common item
    "допущенных к торгам",             # routine listing step (real inflected form, not "допущены")
    "о включении ценных бумаг",        # routine sector/list inclusion (real: "О включении", not "включены")
    "о внесении изменений в список",   # routine list amendment
    "итоги выпуска",                   # routine placement-results digest
    "об оставлении ценных бумаг",      # routine "stays listed" notice
    "о регистрации выпуска биржевых облигаций",  # routine new-issue registration step
    "о регистрации программы",         # routine
    "о регистрации проспекта",         # routine
    "о проведении аукциона",           # routine auction notice -- not observed in the live sample (255
                                        # items, 0 auctions), kept defensively per the task's original list
]

# CBR: only "ключевая ставка" was confirmed live (as "снизить ключевую
# ставку" -- accusative case, hence both forms listed). "банкротство"/
# "отзыв лицензии"/"санкции" were NOT observed in the live 8-item window
# (no license-revocation/sanctions release was current) -- kept per known
# CBR press convention but unconfirmed against a live example; flagged here
# rather than silently presented as verified.
CBR_PRIORITY_PATTERNS = [
    "ключевая ставка", "ключевую ставку", "ключевой ставки",  # rate-decision case variants (accusative confirmed live)
    "key rate",
    "банкротство",           # unconfirmed live -- no live example in the fetched window
    "отзыв лицензии", "отозвал лицензию",  # unconfirmed live -- both noun/verb forms listed since case wasn't observable
    "санкции",               # unconfirmed live
]

CBR_NOISE_PATTERNS = [
    "валютный курс",         # not observed in RssPress (this is the press-release feed, not the daily FX feed) -- kept per task spec
    "официальный курс",
    "результаты мониторинга максимальных процентных ставок",  # real: recurring biweekly deposit-rate digest, 3 of 8 live items
]


def queue_bucket(title: str | None, source: str | None = None) -> str:
    """One of 'priority' / 'noise' / 'normal', case-insensitive substring
    match. Priority always overrides noise -- a title matching both lists
    (e.g. a coupon notice that also mentions a rating action) is never
    silently buried as noise.

    `source` selects which pattern lists apply -- 'moex_listing'/'cbr_press'
    use their own lists above; anything else (None, 'azipi', 'smartlab*',
    unrecognized) keeps the original AZIPI-oriented PRIORITY_PATTERNS/
    NOISE_PATTERNS unchanged, exactly as before this parameter existed."""
    if not title:
        return "normal"
    t = title.lower()
    if source == "moex_listing":
        priority, noise = MOEX_LISTING_PRIORITY_PATTERNS, MOEX_LISTING_NOISE_PATTERNS
    elif source == "cbr_press":
        priority, noise = CBR_PRIORITY_PATTERNS, CBR_NOISE_PATTERNS
    else:
        priority, noise = PRIORITY_PATTERNS, NOISE_PATTERNS
    if any(p in t for p in priority):
        return "priority"
    if any(p in t for p in noise):
        return "noise"
    return "normal"


def is_likely_noise(title: str, source: str | None = None) -> bool:
    """True if `title` matches a noise pattern and no priority pattern.
    Thin wrapper over queue_bucket() -- the exact signature this task's
    Part 2 asks for."""
    return queue_bucket(title, source) == "noise"
