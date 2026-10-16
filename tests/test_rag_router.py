from hybond.rag.router import classify_query


def test_query_router_selects_safe_paths():
    assert classify_query("Какой купон и дата погашения?") == "numeric_fact"
    assert classify_query("Почему вырос спред и что изменилось?") == "mixed"
    assert classify_query("Какие риски у эмитента на данный момент?") == "current_risk"
    assert classify_query("Расскажи об эмитенте и его облигациях.") == "issuer_brief"
    assert classify_query("Покажи финансовые показатели эмитента за последний период.") == "issuer_brief"
    assert classify_query("Сделай профиль облигации RU000A108991.") == "bond_brief"
    assert classify_query("Что произошло с выплатой купона?") == "numeric_fact"
    assert classify_query("Какая погода сегодня?") == "out_of_scope"
    assert classify_query("Что говорится о ликвидности в отчёте?") == "narrative"
