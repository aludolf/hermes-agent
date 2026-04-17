"""Tests for the CredentialsStore (022 — encrypted sentinel credentials)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from agent.orchestrator.credentials import (
    CredentialsStore,
    CredentialsStoreError,
)
from agent.orchestrator.models import CredentialKind
from hermes_state import SessionDB


@pytest.fixture
def master_key() -> str:
    return Fernet.generate_key().decode("utf-8")


@pytest.fixture
def store(tmp_path, master_key) -> tuple[SessionDB, CredentialsStore]:
    db = SessionDB(db_path=tmp_path / "state.db")
    cs = CredentialsStore(db, master_key=master_key)
    yield db, cs
    db.close()


# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------

def test_fails_closed_when_key_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_MASTER_KEY", raising=False)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        with pytest.raises(CredentialsStoreError) as exc:
            CredentialsStore(db)
        assert "HERMES_MASTER_KEY" in str(exc.value)
    finally:
        db.close()


def test_fails_closed_on_invalid_key(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        with pytest.raises(CredentialsStoreError) as exc:
            CredentialsStore(db, master_key="not-a-valid-fernet-key")
        assert "Invalid" in str(exc.value)
    finally:
        db.close()


def test_reads_key_from_env(tmp_path, monkeypatch, master_key):
    monkeypatch.setenv("HERMES_MASTER_KEY", master_key)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        cs = CredentialsStore(db)
        cid = cs.put(kind="imap_app_password", secret="hunter2")
        assert cs.get(cid) == "hunter2"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# put / get round-trip
# ---------------------------------------------------------------------------

def test_put_get_round_trip(store):
    db, cs = store
    cid = cs.put(
        kind=CredentialKind.IMAP_APP_PASSWORD,
        secret="correct-horse-battery-staple",
        label="test gmail",
    )
    assert cid.startswith("cred_")
    assert cs.get(cid) == "correct-horse-battery-staple"


def test_put_rejects_unknown_kind(store):
    _, cs = store
    with pytest.raises(CredentialsStoreError) as exc:
        cs.put(kind="bogus_kind", secret="x")
    assert "unknown credential kind" in str(exc.value)


def test_put_rejects_empty_secret(store):
    _, cs = store
    with pytest.raises(CredentialsStoreError):
        cs.put(kind="imap_app_password", secret="")


def test_get_missing_raises(store):
    _, cs = store
    with pytest.raises(CredentialsStoreError) as exc:
        cs.get("cred_missing")
    assert "not found" in str(exc.value)


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------

def test_tampered_ciphertext_fails_decrypt(store):
    db, cs = store
    cid = cs.put(kind="imap_app_password", secret="secret")

    # Mutate one byte in the stored ciphertext — Fernet MAC must reject.
    row = db.get_credential_ciphertext(cid)
    tampered = bytes(row["ciphertext"])
    tampered = tampered[:-4] + bytes([tampered[-4] ^ 0x01]) + tampered[-3:]
    db.rotate_credential(cid, tampered)

    with pytest.raises(CredentialsStoreError):
        cs.get(cid)


# ---------------------------------------------------------------------------
# Rotate / delete
# ---------------------------------------------------------------------------

def test_rotate_updates_ciphertext_and_timestamp(store):
    db, cs = store
    cid = cs.put(kind="imap_app_password", secret="old")
    cs.rotate(cid, "new")
    assert cs.get(cid) == "new"
    row = db.get_credential_ciphertext(cid)
    assert row["rotated_at"] is not None


def test_rotate_rejects_empty_secret(store):
    _, cs = store
    cid = cs.put(kind="imap_app_password", secret="old")
    with pytest.raises(CredentialsStoreError):
        cs.rotate(cid, "")


def test_rotate_requires_existing_credential(store):
    _, cs = store
    with pytest.raises(CredentialsStoreError):
        cs.rotate("cred_missing", "x")


def test_delete(store):
    _, cs = store
    cid = cs.put(kind="imap_app_password", secret="x")
    cs.delete(cid)
    with pytest.raises(CredentialsStoreError):
        cs.get(cid)


# ---------------------------------------------------------------------------
# Metadata listing must not leak plaintext or ciphertext
# ---------------------------------------------------------------------------

def test_list_metadata_omits_secrets(store):
    _, cs = store
    cs.put(kind="imap_app_password", secret="s1", label="a")
    cs.put(kind="ms_graph_refresh_token", secret="s2", label="b")
    meta = cs.list_metadata()
    assert len(meta) == 2
    for row in meta:
        assert "ciphertext" not in row
        assert "s1" not in str(row) and "s2" not in str(row)

    filtered = cs.list_metadata(kind="imap_app_password")
    assert len(filtered) == 1
    assert filtered[0]["label"] == "a"


# ---------------------------------------------------------------------------
# Multiple instances sharing the same key decrypt each other's output
# ---------------------------------------------------------------------------

def test_multiple_instances_same_key_round_trip(tmp_path, master_key):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        cs1 = CredentialsStore(db, master_key=master_key)
        cid = cs1.put(kind="imap_app_password", secret="sekret")

        cs2 = CredentialsStore(db, master_key=master_key)
        assert cs2.get(cid) == "sekret"
    finally:
        db.close()


def test_different_key_cannot_decrypt(tmp_path, master_key):
    other_key = Fernet.generate_key().decode("utf-8")
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        cs_original = CredentialsStore(db, master_key=master_key)
        cid = cs_original.put(kind="imap_app_password", secret="s")

        cs_attacker = CredentialsStore(db, master_key=other_key)
        with pytest.raises(CredentialsStoreError):
            cs_attacker.get(cid)
    finally:
        db.close()
