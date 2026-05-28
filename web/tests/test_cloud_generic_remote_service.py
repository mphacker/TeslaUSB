"""Tests for ``teslausb_web.services.cloud_generic_remote_service``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from teslausb_web.services.cloud_generic_remote_service import (
    GenericRemoteError,
    GenericRemoteService,
    default_obscure_keys,
    parse_config_block,
    render_conf_body,
    supported_types,
)


def _stub_obscure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the rclone obscure subprocess with a deterministic fake."""

    def _fake_run(cmd, capture_output, text, timeout, check):  # noqa: ANN001
        assert cmd[1] == "obscure"
        plaintext = cmd[2]

        class _R:
            returncode = 0
            stdout = f"OBSC({plaintext})"
            stderr = ""

        return _R()

    monkeypatch.setattr(
        "teslausb_web.services.cloud_generic_remote_service.subprocess.run", _fake_run
    )


def test_supported_types_includes_all_dropdown_backends() -> None:
    types = set(supported_types())
    assert {"sftp", "webdav", "smb", "ftp", "s3", "b2", "wasabi", "azureblob", "swift"} <= types


def test_default_obscure_keys_protects_password_backends() -> None:
    for backend in ("sftp", "webdav", "smb", "ftp"):
        assert default_obscure_keys(backend) == ("pass",)
    for backend in ("s3", "b2", "wasabi", "azureblob", "swift"):
        assert default_obscure_keys(backend) == ()


def test_parse_config_block_section_header() -> None:
    parsed = parse_config_block("[my-nas]\ntype = sftp\nhost = nas.local\nuser = pi\npass = abc\n")
    assert parsed == {"type": "sftp", "host": "nas.local", "user": "pi", "pass": "abc"}


def test_parse_config_block_bare_keys() -> None:
    parsed = parse_config_block("type=s3\nprovider=Other\naccess_key_id=AK\nsecret_access_key=S\n")
    assert parsed["type"] == "s3"
    assert parsed["access_key_id"] == "AK"


def test_parse_config_block_rejects_multiple_sections() -> None:
    with pytest.raises(GenericRemoteError, match="at most one"):
        parse_config_block("[a]\ntype=sftp\n[b]\ntype=ftp\n")


def test_parse_config_block_rejects_missing_type() -> None:
    with pytest.raises(GenericRemoteError, match="missing required 'type'"):
        parse_config_block("host=x\nuser=y\n")


def test_parse_config_block_rejects_unknown_type() -> None:
    with pytest.raises(GenericRemoteError, match="not in the supported"):
        parse_config_block("type=bogus\n")


def test_parse_config_block_rejects_invalid_line() -> None:
    with pytest.raises(GenericRemoteError, match="invalid rclone config line"):
        parse_config_block("type=sftp\nnoequals\n")


def test_import_form_s3_persists_cleartext_secret(tmp_path: Path) -> None:
    svc = GenericRemoteService(tmp_path / "store.json")
    record = svc.import_form(
        "s3",
        {"provider": "Other", "access_key_id": "AK", "secret_access_key": "S"},
    )
    assert record["type"] == "s3"
    assert record["secret_access_key"] == "S"  # S3 backends store cleartext
    assert record["_source"] == "form"
    on_disk = json.loads((tmp_path / "store.json").read_text(encoding="utf-8"))
    assert on_disk["access_key_id"] == "AK"


def test_import_form_sftp_obscures_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_obscure(monkeypatch)
    svc = GenericRemoteService(tmp_path / "store.json")
    record = svc.import_form("sftp", {"host": "nas.local", "user": "pi", "pass": "secret"})
    assert record["pass"] == "OBSC(secret)"
    assert "pass" in record["_obscure_keys"]


def test_import_form_explicit_empty_obscure_keys_skips_obscuring(
    tmp_path: Path,
) -> None:
    # When obscure_keys=[] is passed explicitly, the password is stored
    # verbatim — useful for paste flows where the value is already obscured.
    svc = GenericRemoteService(tmp_path / "store.json")
    record = svc.import_form(
        "sftp", {"host": "x", "user": "y", "pass": "already-obscured"}, obscure_keys=[]
    )
    assert record["pass"] == "already-obscured"


def test_import_form_rejects_control_chars_in_value(tmp_path: Path) -> None:
    svc = GenericRemoteService(tmp_path / "store.json")
    with pytest.raises(GenericRemoteError, match="forbidden control character"):
        svc.import_form("s3", {"endpoint": "x\ntype = local"})


def test_import_form_rejects_type_in_fields(tmp_path: Path) -> None:
    svc = GenericRemoteService(tmp_path / "store.json")
    with pytest.raises(GenericRemoteError, match="'type' is set from rclone_type"):
        svc.import_form("s3", {"type": "swift"})


def test_import_form_rejects_reserved_underscore_key(tmp_path: Path) -> None:
    svc = GenericRemoteService(tmp_path / "store.json")
    with pytest.raises(GenericRemoteError, match="reserved"):
        svc.import_form("s3", {"_source": "spoof"})


def test_import_form_rejects_unsupported_type(tmp_path: Path) -> None:
    svc = GenericRemoteService(tmp_path / "store.json")
    with pytest.raises(GenericRemoteError, match="not in the supported"):
        svc.import_form("bogus", {})


def test_import_config_block_round_trips_via_load(tmp_path: Path) -> None:
    svc = GenericRemoteService(tmp_path / "store.json")
    block = "[my-nas]\ntype = sftp\nhost = nas.local\nuser = pi\npass = already-obscured\n"
    record = svc.import_config_block(block)
    assert record["type"] == "sftp"
    assert record["pass"] == "already-obscured"  # paste path skips re-obscure
    loaded = svc.load()
    assert loaded is not None
    assert loaded["host"] == "nas.local"
    assert loaded["_source"] == "paste"


def test_load_returns_none_when_no_file(tmp_path: Path) -> None:
    svc = GenericRemoteService(tmp_path / "missing.json")
    assert svc.load() is None


def test_clear_removes_file(tmp_path: Path) -> None:
    svc = GenericRemoteService(tmp_path / "store.json")
    svc.import_form("s3", {"access_key_id": "AK", "secret_access_key": "S"})
    assert svc.clear() is True
    assert svc.load() is None
    assert svc.clear() is False  # idempotent


def test_render_conf_body_emits_rclone_block(tmp_path: Path) -> None:
    record = {
        "type": "s3",
        "access_key_id": "AK",
        "secret_access_key": "S",
        "_source": "form",
        "_obscure_keys": "",
    }
    text = render_conf_body(record, remote_name="teslausb")
    assert text.startswith("[teslausb]\ntype = s3\n")
    assert "access_key_id = AK" in text
    assert "_source" not in text
    assert "_obscure_keys" not in text


def test_render_conf_body_rejects_record_without_type() -> None:
    with pytest.raises(GenericRemoteError, match="missing 'type'"):
        render_conf_body({"host": "x"})
