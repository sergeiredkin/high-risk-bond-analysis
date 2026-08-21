"""Automated event classification via local Ollama (qwen3.5:latest) -- a
lower-latency, no-manual-session stand-in for the hy-event-classify skill.

Deliberately reuses hy-event-classify's OWN taxonomy (see
skills/hy-event-classify/references/taxonomy.md), not an invented one --
category/severity values land in the same `event` table as 2000+ manually
classified rows, and the dashboard's SEV_COLOR/EVENT_CATEGORY_RU maps
already key on these exact strings (medium included). A different scheme
here would silently fragment that one dataset into two incompatible ones.

`affected_pillar`/`direction`/`also`/`so_what`/`note_ru` from the skill's
full output contract are intentionally NOT requested here -- the `event`
table has no columns for them (see PRAGMA table_info(event)), so asking the
model for fields nothing stores would just be dead weight in the prompt.
"""
from __future__ import annotations

import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:latest")

CONFIDENCE_THRESHOLD = 0.65
REVIEW_CONFIDENCE_THRESHOLD = 0.80

# Same 10 categories / 5 severities as skills/hy-event-classify/references/
# taxonomy.md -- 'routine' (not 'noise') is the category for routine
# disclosures; 'noise' is only ever a severity.
VALID_CATEGORIES = {
    "reporting_delay", "coupon_event", "offer_put_call", "covenant",
    "rating_action", "governance", "insolvency", "operations",
    "capital_action", "routine",
}
VALID_SEVERITIES = {"noise", "low", "medium", "high", "critical"}

PROMPT_TEMPLATE = """/no_think
Ты — аналитик российского рынка высокодоходных облигаций (ВДО). Классифицируй раскрытие информации эмитента по таксономии режимов дефолта российских ВДО-эмитентов.

Эмитент: {issuer_name}
Заголовок: {title}
Источник: {url}

Категории (ровно одна, самая существенная, если раскрытие затрагивает несколько):
- reporting_delay: задержка/непредоставление отчётности РСБУ/МСФО, пропущенные сроки раскрытия
- coupon_event: установление/пересмотр ставки купона, выплата купона, ПРОПУСК купона, технический дефолт по купону
- offer_put_call: оферта (put), колл, изменение графика амортизации
- covenant: нарушение ковенанта, waiver, кросс-дефолт
- rating_action: присвоение/изменение/отзыв кредитного рейтинга (АКРА/Эксперт РА/НКР/НРА)
- governance: смена гендиректора/финдиректора/совета директоров, смена аудитора, сделки со связанными сторонами, смена бенефициара, судебные иски к эмитенту
- insolvency: банкротство, реструктуризация, мораторий, арест имущества, взыскание
- operations: остановка производства, потеря крупного клиента/контракта, продажа активов, санкции против эмитента
- capital_action: новый выпуск облигаций, банковское финансирование, взнос в капитал, объявление/изменение дивидендов
- routine: плановые раскрытия без кредитного содержания — списки аффилированных лиц, уведомления об ОСА, стандартная квартальная отчётность, подтверждающая ожидания

Правила severity (critical > high > medium > low > noise):
- noise: ТОЛЬКО для category=routine, и для планового купона, выплаченного по графику
- low: повышение рейтинга, благоприятный контракт
- medium: обычная ротация совета директоров, снижение прогноза (guidance), типовое capital_action (новый выпуск, финансирование)
- high: понижение рейтинга/негативный прогноз, отставка аудитора, внезапный уход финдиректора, waiver по ковенанту, крупный дивиденд у закредитованного эмитента, оферта/put, потеря ключевого клиента, санкции
- critical: пропуск купона/технический дефолт, нарушение ковенанта/кросс-дефолт, банкротство/реструктуризация/арест имущества, многоступенчатое понижение или отзыв рейтинга, крупный судебный иск, непонятная/повторная задержка отчётности

Верни ТОЛЬКО валидный JSON без комментариев и markdown:
{{
  "category": "<одна из категорий выше>",
  "severity": "<noise|low|medium|high|critical>",
  "confidence": <число от 0.0 до 1.0>,
  "summary": "<одно предложение на русском, что произошло>"
}}
"""


def classify_filing(title: str, url: str, issuer_name: str) -> dict | None:
    """Send a filing to local Ollama for classification.

    Returns dict with keys: category, severity, confidence, summary.
    Returns None on failure (network error, timeout, invalid JSON, or a
    value outside the taxonomy) -- the filing then stays unclassified for
    manual review rather than getting a garbage row.
    """
    prompt = PROMPT_TEMPLATE.format(issuer_name=issuer_name, title=title, url=url or "")

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,  # parse the full response at once, not a token stream
        "think": False,  # top-level field per Ollama's /api/generate contract --
        # nesting this under "options" is silently rejected ("invalid option
        # provided option=think" server-side), so the model always ran its
        # full reasoning chain and blew the context budget before emitting
        # JSON. That misplacement, not a genuine model/timeout limitation,
        # was the real cause of the empty-response failures seen all session.
        "options": {
            "temperature": 0.1,  # kept well under 0.2 -- higher produces inconsistent JSON
            "top_k": 20,
            "top_p": 0.95,
        },
    }

    try:
        # Timeout is 120s, not the originally-specified 30s: measured against
        # the real model on this machine (RTX 3060), a single classification
        # call with this taxonomy-sized prompt takes 52-77s even with
        # think=False -- qwen3.5's "thinking" capability isn't fully
        # suppressed by this Ollama build for a task this complex, it just
        # runs a genuine multi-step reasoning chain before answering. At 30s
        # essentially every real call would time out and nothing would ever
        # get classified; 120s gives ~1.5x margin over the observed range.
        resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
        resp.raise_for_status()
        raw = resp.json()["response"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return _validate_result(json.loads(raw))
    except requests.exceptions.Timeout:
        logger.warning("Ollama timeout (120s) — skipping: %s", title)
        return None
    except requests.exceptions.RequestException as e:
        logger.warning("Ollama request failed (%s) — skipping: %s", e, title)
        return None
    except (KeyError, json.JSONDecodeError, AssertionError, ValueError, IndexError) as e:
        logger.warning("Ollama response invalid (%s) — skipping: %s", e, title)
        return None


BATCH_PROMPT_TEMPLATE = """/no_think
Ты — аналитик российского рынка высокодоходных облигаций (ВДО). Классифицируй КАЖДОЕ из перечисленных ниже раскрытий информации по таксономии режимов дефолта российских ВДО-эмитентов.

Раскрытия:
{items_block}

Категории (ровно одна на раскрытие, самая существенная, если раскрытие затрагивает несколько):
- reporting_delay: задержка/непредоставление отчётности РСБУ/МСФО, пропущенные сроки раскрытия
- coupon_event: установление/пересмотр ставки купона, выплата купона, ПРОПУСК купона, технический дефолт по купону
- offer_put_call: оферта (put), колл, изменение графика амортизации
- covenant: нарушение ковенанта, waiver, кросс-дефолт
- rating_action: присвоение/изменение/отзыв кредитного рейтинга (АКРА/Эксперт РА/НКР/НРА)
- governance: смена гендиректора/финдиректора/совета директоров, смена аудитора, сделки со связанными сторонами, смена бенефициара, судебные иски к эмитенту
- insolvency: банкротство, реструктуризация, мораторий, арест имущества, взыскание
- operations: остановка производства, потеря крупного клиента/контракта, продажа активов, санкции против эмитента
- capital_action: новый выпуск облигаций, банковское финансирование, взнос в капитал, объявление/изменение дивидендов
- routine: плановые раскрытия без кредитного содержания — списки аффилированных лиц, уведомления об ОСА, стандартная квартальная отчётность, подтверждающая ожидания

Правила severity (critical > high > medium > low > noise):
- noise: ТОЛЬКО для category=routine, и для планового купона, выплаченного по графику
- low: повышение рейтинга, благоприятный контракт
- medium: обычная ротация совета директоров, снижение прогноза (guidance), типовое capital_action (новый выпуск, финансирование)
- high: понижение рейтинга/негативный прогноз, отставка аудитора, внезапный уход финдиректора, waiver по ковенанту, крупный дивиденд у закредитованного эмитента, оферта/put, потеря ключевого клиента, санкции
- critical: пропуск купона/технический дефолт, нарушение ковенанта/кросс-дефолт, банкротство/реструктуризация/арест имущества, многоступенчатое понижение или отзыв рейтинга, крупный судебный иск, непонятная/повторная задержка отчётности

Верни ТОЛЬКО валидный JSON-массив, ровно {n} элементов, в ТОМ ЖЕ порядке, что и раскрытия выше, без комментариев и markdown:
[
  {{"category": "<...>", "severity": "<noise|low|medium|high|critical>", "confidence": <0.0-1.0>, "summary": "<одно предложение на русском>"}},
  ...
]
"""


def _validate_result(result: dict) -> dict:
    """Shared per-item validation -- same rules classify_filing() applies,
    factored out so classify_filings_batch() can apply them to each array
    element independently (one malformed item must not discard the batch)."""
    assert result["category"] in VALID_CATEGORIES
    assert result["severity"] in VALID_SEVERITIES
    assert 0.0 <= float(result["confidence"]) <= 1.0
    assert isinstance(result["summary"], str) and len(result["summary"]) > 5
    result["confidence"] = float(result["confidence"])
    return result


def classify_filings_batch(filings: list[dict]) -> list[dict | None]:
    """Classify multiple filings in a single Ollama call -- the theory being
    one round-trip instead of len(filings) would cut wall-clock time, since
    52-77s of real inference latency per call (not token count) is what
    dominates a single classify_filing() call (see its timeout comment).

    MEASURED AND NOT CURRENTLY VIABLE on this machine's real Ollama/qwen3.5
    build, as of the 2026-08-20 test: two live batch calls (2 items, then 5
    items, up to num_predict=4000) both came back with done_reason='length'
    and an EMPTY response after ~75s. CAVEAT (2026-08-21): that test ran with
    `think: False` misplaced inside `options` instead of top-level in the
    payload, which Ollama silently ignores ("invalid option provided
    option=think" server-side) -- so the model was doing full unsuppressed
    reasoning the whole time, not "think:False failing to suppress
    reasoning" as originally concluded. Both call sites now send `think` at
    the top level (see classify_filing()'s payload comment). The batch
    finding above has NOT been re-measured against the fix and may no longer
    hold -- re-test live before drawing new conclusions either way.
    cmd_auto_classify does NOT call this function as a result -- it stays
    single-item (classify_filing per filing, batched only for DB-commit
    cadence). Kept here, unit-tested, in case a re-measurement shows this
    becomes viable now that the payload bug is fixed.

    `filings`: list of dicts with title/url/issuer_name keys (the same
    shape get_auto_classify_queue()/get_auto_classify_market_notes() rows
    already have). Returns a list the same length as `filings`, each entry
    either a validated result dict or None (that item failed -- stays
    unclassified for manual/later review, exactly like classify_filing()'s
    single-item None case; one bad item in the array does not discard the
    rest of the batch).
    """
    if not filings:
        return []
    items_block = "\n".join(
        f"{i}. Эмитент: {f['issuer_name']}\n   Заголовок: {f['title']}\n   Источник: {f.get('url') or ''}"
        for i, f in enumerate(filings, start=1))
    prompt = BATCH_PROMPT_TEMPLATE.format(items_block=items_block, n=len(filings))

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "think": False,  # top-level field, see classify_filing()'s payload comment
        "options": {"temperature": 0.1, "top_k": 20, "top_p": 0.95},
    }

    try:
        # Same 120s-per-call budget as classify_filing() -- a batch call is
        # one round-trip, not len(filings) of them, so this is NOT scaled up
        # by batch size; if a larger batch genuinely needs longer than a
        # single-item call, that's exactly the signal that batch size is too
        # big and should come back down, not that the timeout should grow.
        resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
        resp.raise_for_status()
        raw = resp.json()["response"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
        assert isinstance(parsed, list)
    except requests.exceptions.Timeout:
        logger.warning("Ollama batch timeout (120s) — skipping %d filing(s)", len(filings))
        return [None] * len(filings)
    except requests.exceptions.RequestException as e:
        logger.warning("Ollama batch request failed (%s) — skipping %d filing(s)", e, len(filings))
        return [None] * len(filings)
    except (KeyError, json.JSONDecodeError, AssertionError, ValueError, IndexError) as e:
        logger.warning("Ollama batch response invalid (%s) — skipping %d filing(s)", e, len(filings))
        return [None] * len(filings)

    out: list[dict | None] = []
    for i in range(len(filings)):
        if i >= len(parsed):
            out.append(None)  # model returned fewer items than asked -- pad with None
            continue
        try:
            out.append(_validate_result(dict(parsed[i])))
        except (KeyError, AssertionError, ValueError, TypeError) as e:
            logger.warning("Batch item %d invalid (%s) — skipping: %s", i, e, filings[i].get("title"))
            out.append(None)
    return out


def needs_review(severity: str, confidence: float) -> bool:
    """critical/high ALWAYS needs review regardless of confidence -- the
    most important signals must never be auto-accepted unseen. Otherwise,
    anything below REVIEW_CONFIDENCE_THRESHOLD (except noise, which is
    reviewed via the ordinary event feed, not this queue) is flagged too."""
    if severity in ("high", "critical"):
        return True
    if confidence < REVIEW_CONFIDENCE_THRESHOLD and severity != "noise":
        return True
    return False
