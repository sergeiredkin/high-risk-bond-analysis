# Golden set v1 evidence review

**Review date:** 2026-10-09  
**Reviewer:** local coding agent; evidence manually checked against indexed page text  
**Scope:** `eval/golden/ru_bonds_v1.jsonl` and `eval/corpus/manifest_v1.jsonl`

This is an evidence audit, not a frozen production benchmark. It checks that answerable expected values are supported by the referenced page and records corpus-bounded abstention cases. It does not establish global absence outside the small manifest.

| QID | Review | Evidence / issue |
|---|---|---|
| GS-0001 | Supported | ISIN `RU000A10AHU1` appears on the cited issuer-disclosure page. |
| GS-0002 | Supported | Page explicitly says the issuer failed to pay coupon income; Boolean expected `true` is correct. |
| GS-0003 | Supported | Page gives `67 800 000,00 руб.`; numeric evaluator now parses grouped spaces and decimal comma. |
| GS-0004 | Supported | Page explicitly states «отсутствие денежных средств на расчетном счете». |
| GS-0005 | Supported | Page gives coupon-period end date `21.09.2026`; expected ISO date is equivalent. |
| GS-0006 | Supported | Issuer name «Уральская Сталь» is in the disclosure header. |
| GS-0007 | Supported | INN `7826705374` is in the disclosure header. |
| GS-0008 | Corpus-bounded abstention | No evidence found in the referenced v1 corpus; search abstained. Does not prove absence from all HyBond documents. |
| GS-0009 | Corpus-bounded abstention; retrieval failure | No supporting issue-volume evidence in v1, but retrieval returned an irrelevant page. |
| GS-0010 | Corpus-bounded abstention; retrieval failure | No supporting maturity evidence in v1, but retrieval returned an irrelevant page. |
| GS-0011 | Supported as reference only | `spravka_vdo.md` defines G-spread as the YTM difference versus comparable OFZ yield. It is not authoritative issuer evidence. |
| GS-0012 | Supported as reference only | Reference page defines YTM as the discount rate equating present value of cash flows to market price. |
| GS-0013 | Supported as reference only | The cited sentence appears verbatim. This is methodology/opinion, not an issuer-specific fact. |
| GS-0014 | Supported as reference only | The page says looking only at the rating letter is «недостаточно»; expected Boolean `false` is correct. |
| GS-0015 | Corpus-bounded abstention; retrieval failure | The exact 2028 exchange forecast is absent from the reference page, but retrieval returned unrelated pages. |

## Baseline snapshot

Command: `hybond docs eval --golden eval/golden/ru_bonds_v1.jsonl --manifest eval/corpus/manifest_v1.jsonl --k 5`

| Metric | Result |
|---|---:|
| Page recall@5 | 0.733 |
| Citation precision@5 | 0.582 |
| MRR | 0.733 |
| Answer/evidence match | 0.364 |
| Abstention accuracy | 0.250 |

These metrics confirm that v1 is not production-grade: 7 narrative answers need human scoring, and three unanswerable prompts retrieve irrelevant pages. The set also covers only three issuers, is small, and has no scanned/table-heavy fixtures. Do not freeze it as the final benchmark or use it to claim answer quality.

The numeric matcher was corrected to recognize Russian grouped amounts (`67 800 000,00`) and the Boolean evidence matcher recognizes the explicit Russian formulations used by this set. Retrieval abstention still needs a calibrated relevance threshold evaluated on a larger, manually reviewed set.

## Next evaluation work

1. Expand to 60–100 questions across at least 10 issuers.
2. Include genuinely unanswerable, table, scanned-PDF, cross-document, and conflicting-source cases.
3. Record exact source/page/quote, issuer scope, answer type, reviewer, and review date for every item.
4. Human-review this agent-checked draft before freezing a benchmark; retain a separate frozen test split.
