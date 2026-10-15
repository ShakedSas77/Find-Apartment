

def test_tel_aviv_focus_neighborhood_anchors_to_tel_aviv():
    from maps import _anchor_default_city
    assert _anchor_default_city("נחלת יצחק") == "נחלת יצחק, תל אביב, ישראל"


def test_unnamed_city_still_defaults_to_ramat_gan_givatayim():
    from maps import _anchor_default_city
    assert _anchor_default_city("הרצל 5") == "הרצל 5, רמת גן, גבעתיים, ישראל"


def test_tel_aviv_focus_neighborhood_south_of_the_cutoff_is_excluded(monkeypatch):
    import config
    import maps

    monkeypatch.setattr(maps.storage, "get_address_cache", lambda address: None)
    monkeypatch.setattr(maps.storage, "save_address_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(maps, "_validate_address_with_geocoding",
                        lambda address, confidence: (address, "תל אביב", "high", "", "ok", config.EXCLUDE_SOUTH_OF_LAT - 0.01, 34.79))

    assert maps.get_walking_distance("נחלת יצחק, תל אביב")[4] == "excluded_south"
