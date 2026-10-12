from pev_buddy.text import fold


def test_fold():
    assert fold("Café Encore") == fold("CAFE ENCORE") == "cafe encore"
    assert fold("Crème Brûlée") == "creme brulee"
    assert fold(None) == fold("") == ""
