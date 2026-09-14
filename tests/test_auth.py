from pathlib import Path

import pytest

from collective_alpha.data.massive import auth


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "s3.txt"
    p.write_text(text)
    return p


def test_api_key_plain(tmp_path):
    p = tmp_path / "k.txt"
    p.write_text("abc123\n")
    assert auth.load_api_key(p) == "abc123"


def test_api_key_kv(tmp_path):
    p = tmp_path / "k.txt"
    p.write_text("MASSIVE_API_KEY=abc123\n")
    assert auth.load_api_key(p) == "abc123"


def test_s3_two_lines(tmp_path):
    c = auth.load_s3_credentials(_write(tmp_path, "ID123\nSECRET456\n"))
    assert (c.access_key_id, c.secret_access_key) == ("ID123", "SECRET456")


def test_s3_kv(tmp_path):
    c = auth.load_s3_credentials(_write(tmp_path, "aws_secret_access_key = S\naws_access_key_id: I\n"))
    assert (c.access_key_id, c.secret_access_key) == ("I", "S")


def test_s3_dashboard_paste(tmp_path):
    text = "Access Key ID\nabcdef-1234\nSecret Access Key\nxyz789\nS3 Endpoint\nhttps://files.massive.com\n"
    c = auth.load_s3_credentials(_write(tmp_path, text))
    assert (c.access_key_id, c.secret_access_key) == ("abcdef-1234", "xyz789")


def test_s3_json(tmp_path):
    c = auth.load_s3_credentials(_write(tmp_path, '{"access_key_id": "I", "secret_access_key": "S"}'))
    assert (c.access_key_id, c.secret_access_key) == ("I", "S")


def test_s3_single_id_falls_back_to_api_key(tmp_path):
    c = auth.load_s3_credentials(_write(tmp_path, "ONLYID\n"), fallback_secret="APIKEY")
    assert (c.access_key_id, c.secret_access_key) == ("ONLYID", "APIKEY")


def test_s3_unparseable(tmp_path):
    with pytest.raises(ValueError):
        auth.load_s3_credentials(_write(tmp_path, "ONLYID\n"))


def test_redact():
    assert auth.redact("abcdefghijkl") == "abcd…jkl"
    assert auth.redact("short") == "***"
