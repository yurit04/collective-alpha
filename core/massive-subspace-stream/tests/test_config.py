from pathlib import Path

import pytest
from massive_subspace_stream import ConfigError, load_settings
from massive_subspace_stream._config import parse_env_file


def test_parse_env_file_handles_export_quotes_and_comments(tmp_path):
    env = tmp_path / "x.env"
    env.write_text(
        "# comment\n"
        "export MASSIVE_API_KEY=\"abc123\"\n"
        "MASSIVE_S3_BUCKET='flatfiles'\n"
        "PLAIN=value # trailing\n"
        "MASSIVE_S3_ACCESS_KEY_ID=ffc3-1\n"
        "not a pair\n"
    )
    values = parse_env_file(env)
    assert values == {"MASSIVE_API_KEY": "abc123", "MASSIVE_S3_BUCKET": "flatfiles", "PLAIN": "value",
                      "MASSIVE_S3_ACCESS_KEY_ID": "ffc3-1"}


def test_credentials_from_env_file_and_env_precedence(tmp_path, monkeypatch):
    env = tmp_path / "m.env"
    env.write_text('export MASSIVE_API_KEY="from-file"\nexport MASSIVE_S3_ACCESS_KEY_ID="kid"\n'
                   'export MASSIVE_S3_SECRET_ACCESS_KEY="sec"\n')
    s = load_settings(env_file=env)
    assert (s.api_key, s.s3_access_key_id, s.s3_secret_access_key) == ("from-file", "kid", "sec")
    monkeypatch.setenv("MASSIVE_API_KEY", "from-env")
    assert load_settings(env_file=env).api_key == "from-env"


def test_secrets_never_in_repr(tmp_path):
    s = load_settings(data_root=tmp_path, env_file=None, api_key="K-9f8e7d", s3_access_key_id="ID-1a2b",
                      s3_secret_access_key="SK-3c4d")
    text = repr(s)
    assert not any(v in text for v in ("K-9f8e7d", "ID-1a2b", "SK-3c4d"))


def test_toml_config_and_env_override(tmp_path, monkeypatch):
    cfg = tmp_path / "collective.toml"
    cfg.write_text('[massive_subspace_stream]\ndata_root = "~/elsewhere"\ndownload_workers = 8\n'
                   'stream_watchlist = ["aapl", "MSFT"]\n')
    s = load_settings(env_file=None)  # found by walking up from cwd (tmp_path)
    assert s.config_file == cfg
    assert s.data_root == Path("~/elsewhere").expanduser()
    assert s.download_workers == 8
    assert s.stream_watchlist == ("aapl", "MSFT")
    monkeypatch.setenv("SUBSPACE_DATA_ROOT", str(tmp_path / "d"))
    assert load_settings(env_file=None).data_root == tmp_path / "d"


def test_credentials_rejected_in_toml(tmp_path):
    (tmp_path / "collective.toml").write_text('[massive_subspace_stream]\napi_key = "nope"\n')
    with pytest.raises(ConfigError, match="credential"):
        load_settings(env_file=None)


def test_require_helpers(tmp_path):
    s = load_settings(env_file=None, data_root=tmp_path)
    with pytest.raises(ConfigError):
        s.require_api_key()
    with pytest.raises(ConfigError):
        s.require_s3()
