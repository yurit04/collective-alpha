from collective_alpha.storage.manifest import Manifest


def test_manifest_lifecycle(tmp_path):
    m = Manifest(tmp_path / "m.sqlite")
    key = "us_stocks_sip/day_aggs_v1/2024/04/2024-04-05.csv.gz"
    assert not m.is_downloaded(key)
    m.mark_downloaded(key, "flatfile", "day_aggs_v1", "2024-04-05", 123, "etag1")
    assert m.is_downloaded(key)
    assert m.is_downloaded(key, size=123, etag="etag1")
    assert not m.is_downloaded(key, size=999)
    assert not m.is_converted(key)
    assert m.pending_conversion("day_aggs_v1") == [key]
    m.mark_converted(key, tmp_path / "x.parquet", 10)
    assert m.is_converted(key)
    assert m.pending_conversion("day_aggs_v1") == []
    # re-download resets conversion state
    m.mark_downloaded(key, "flatfile", "day_aggs_v1", "2024-04-05", 124, "etag2")
    assert not m.is_converted(key)
    rid = m.start_run("test")
    m.finish_run(rid, "ok")
    rows = m.summary()
    assert rows[0][1] == "day_aggs_v1"
    m.close()
