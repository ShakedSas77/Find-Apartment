

def test_tel_aviv_focus_neighborhood_anchors_to_tel_aviv():
    from maps import _anchor_default_city
    assert _anchor_default_city("נחלת יצחק") == "נחלת יצחק, תל אביב, ישראל"


def test_unnamed_city_still_defaults_to_ramat_gan_givatayim():
    from maps import _anchor_default_city
    assert _anchor_default_city("הרצל 5") == "הרצל 5, רמת גן, גבעתיים, ישראל"
