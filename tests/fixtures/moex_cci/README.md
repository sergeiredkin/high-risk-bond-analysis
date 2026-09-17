Real MOEX ISS CCI rating responses recorded 2026-09-17 (anonymous endpoints
`/iss/cci/rating/companies/ecbd_<emitent_id>.json` and
`.../ecbd_<emitent_id>/securities/isin_<ISIN>.json`), trimmed to
`iss.meta=off` shape. `issuer_empty.json` is the shape of an issuer MOEX
has no ratings for. Used by tests/test_moex_ratings.py.

| file | emitent_id | issuer |
|---|---|---|
| issuer_rzd | 712 | РЖД (АКРА AAA(RU), Эксперт РА ruAAA) |
| issuer_sber_osk | 484 | Сбербанк (national + ОСК books 11/43) |
| issuer_mgkl_withdrawn | 11004 | МГКЛ (Эксперт РА withdrawn 2026-09-14, НРА BB) |
| issuer_anterra_default | 12725 | АНТЕРРА (НРА D, Эксперт РА withdrawn) |
| issuer_centr_rezerv_default | 12366 | Центр-резерв (АКРА D(RU)) |
| issuer_seligdar_split | 4579 | Селигдар (ruA / A+.ru / A+\|ru\|) |
| issuer_x5_finance_withdrawn | 1259 | ИКС 5 ФИНАНС, issuer level: withdrawn only |
| issue_x5_finance | 1259 / RU000A1075S4 | ИКС 5 ФИНАНС issue: three agencies AAA |
| issue_titan_sf | 11269 / RU000A102MB3 | Ипотечный агент Титан-3: AAA(ru.sf) only |
