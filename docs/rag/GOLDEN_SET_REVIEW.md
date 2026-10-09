# Golden set v1 evidence review

**Review date:** 2026-10-09
**Review status:** draft audit; human verification remains required
**Scope:** `eval/golden/ru_bonds_v1.jsonl` and `eval/corpus/manifest_v1.jsonl`

This is an evidence audit, not a frozen production benchmark. It checks that answerable expected values are supported by the referenced page and records corpus-bounded abstention cases. It does not establish global absence outside the small manifest.

| QID | Review | Evidence / issue |
|---|---|---|
| GS-0001 | Candidate evidence match | ISIN `RU000A10AHU1` appears on the cited issuer-disclosure page; verify against the cited corpus before treating as audited. |
| GS-0002 | Candidate evidence match | Page text is described as explicitly stating coupon nonpayment; verify the cited corpus and Boolean interpretation manually. |
| GS-0003 | Candidate evidence match | Expected amount is `67 800 000,00 руб.`; numeric evaluator handles grouped spaces and decimal comma, pending cited-corpus verification. |
| GS-0004 | Candidate evidence match | Reported evidence is «отсутствие денежных средств на расчетном счете»; verify against the cited corpus. |
| GS-0005 | Candidate evidence match | Reported page date is `21.09.2026`, equivalent to the expected ISO date; verify against the cited corpus. |
| GS-0006 | Candidate evidence match | Reported issuer name «Уральская Сталь»; verify the disclosure header in the cited corpus. |
| GS-0007 | Candidate evidence match | Reported INN `7826705374`; verify the disclosure header in the cited corpus. |
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

This reported snapshot does not establish cited-corpus evidence quality; 7 narrative answers still need human scoring, and three unanswerable prompts are reported to retrieve irrelevant pages. The set also covers only three issuers, is small, and has no scanned/table-heavy fixtures. Do not freeze it as the final benchmark or use it to claim answer quality.

The numeric matcher handles Russian grouped amounts (`67 800 000,00`), and Boolean matching recognizes explicit Russian formulations. The cited-corpus audit and retrieval abstention behavior still require manual verification on a larger reviewed set.

## Next evaluation work

1. Expand to 60–100 questions across at least 10 issuers.
2. Include genuinely unanswerable, table, scanned-PDF, cross-document, and conflicting-source cases.
3. Record exact source/page/quote, issuer scope, answer type, reviewer, and review date for every item.
4. Human-review this draft against the cited corpus before freezing any benchmark; retain a separate frozen test split.
