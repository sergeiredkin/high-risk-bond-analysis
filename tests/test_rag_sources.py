from hybond.rag.ingest import classify_source


def test_secondary_commentary_is_not_called_issuer_disclosure():
    assert classify_source("Владислав Кофанов: сделки прошлой недели", "edisclosure", "issuer_disclosure") == (
        "market_commentary", "secondary_commentary"
    )
    assert classify_source("Сообщение эмитента о выплате купона", "edisclosure", "issuer_disclosure") == (
        "edisclosure", "issuer_disclosure"
    )
