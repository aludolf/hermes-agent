"""Tests for EntityContextManager (021 US4)."""

from __future__ import annotations

import json
import time

import pytest

from agent.orchestrator.entity_context import (
    MAX_CONTEXT_SNIPPETS,
    EntityContextManager,
)
from hermes_state import SessionDB


def _mgr(tmp_path) -> tuple[SessionDB, EntityContextManager]:
    db = SessionDB(db_path=tmp_path / "state.db")
    return db, EntityContextManager(db)


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------

def test_update_context_creates_new_entity(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        rec = mgr.update_context(
            name="João", context_snippet="reunião sexta às 10h",
            source_extraction_id="ext_1",
        )
        assert rec is not None
        assert rec["name"] == "João"
        assert rec["mention_count"] == 1
        snippets = json.loads(rec["context_snippets_json"])
        assert len(snippets) == 1
        assert snippets[0]["text"] == "reunião sexta às 10h"
        assert snippets[0]["source_extraction_id"] == "ext_1"
    finally:
        db.close()


def test_update_context_appends_to_existing(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.update_context(name="João", context_snippet="primeira menção")
        mgr.update_context(name="João", context_snippet="segunda menção")
        mgr.update_context(name="joão", context_snippet="terceira (lowercase)")

        rec = mgr.get_entity("JOÃO")
        assert rec is not None
        assert rec["mention_count"] == 3
        snippets = json.loads(rec["context_snippets_json"])
        assert len(snippets) == 3
        assert [s["text"] for s in snippets] == [
            "primeira menção", "segunda menção", "terceira (lowercase)",
        ]
    finally:
        db.close()


def test_update_context_caps_snippets_at_50(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        for i in range(MAX_CONTEXT_SNIPPETS + 10):
            mgr.update_context(name="João", context_snippet=f"snip {i}")
        rec = mgr.get_entity("João")
        snippets = json.loads(rec["context_snippets_json"])
        assert len(snippets) == MAX_CONTEXT_SNIPPETS
        # Oldest ("snip 0"..."snip 9") should be gone; newest retained.
        assert snippets[-1]["text"] == f"snip {MAX_CONTEXT_SNIPPETS + 9}"
        assert snippets[0]["text"] == f"snip 10"
    finally:
        db.close()


def test_update_context_rejects_empty_name(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        assert mgr.update_context(name="", context_snippet="x") is None
        assert mgr.update_context(name="   ", context_snippet="x") is None
    finally:
        db.close()


def test_update_context_unknown_type_defaults_to_person(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        rec = mgr.update_context(
            name="ACME Corp", entity_type="organization",
            context_snippet="cliente novo",
        )
        assert rec is not None
        assert rec["entity_type"] == "person"  # unknown type → default
    finally:
        db.close()


def test_update_context_accepts_known_types(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        rec = mgr.update_context(name="Hermes", entity_type="project",
                                  context_snippet="projeto interno")
        assert rec["entity_type"] == "project"
        rec = mgr.update_context(name="DuckDB", entity_type="tool")
        assert rec["entity_type"] == "tool"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def test_get_entity_case_insensitive(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.update_context(name="João")
        assert mgr.get_entity("joão") is not None
        assert mgr.get_entity("JOÃO") is not None
        assert mgr.get_entity("Maria") is None
    finally:
        db.close()


def test_search_entities_prefix(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.update_context(name="João")
        mgr.update_context(name="Joana")
        mgr.update_context(name="Pedro")
        hits = mgr.search_entities("jo")
        names = {h["name"] for h in hits}
        assert names == {"João", "Joana"}
    finally:
        db.close()


def test_list_all_entities_ordered_by_last_mentioned(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.update_context(name="Alice", context_snippet="a")
        time.sleep(0.01)
        mgr.update_context(name="Bob", context_snippet="b")
        time.sleep(0.01)
        mgr.update_context(name="Alice", context_snippet="a again")

        rows = mgr.list_all_entities()
        names = [r["name"] for r in rows]
        assert names[0] == "Alice"  # most-recently-mentioned first
        assert names[1] == "Bob"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Collision
# ---------------------------------------------------------------------------

def test_detect_collision_returns_true_for_existing(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.update_context(name="João")
        assert mgr.detect_collision("joão") is True
        assert mgr.detect_collision("Pedro") is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def test_format_entity_summary_includes_stats(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.update_context(name="João", context_snippet="reunião")
        mgr.update_context(name="João", context_snippet="ligar")
        out = mgr.format_entity_summary("João")
        assert "João" in out
        assert "2 menções" in out
        assert "reunião" in out
        assert "ligar" in out
    finally:
        db.close()


def test_format_entity_summary_not_found(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        out = mgr.format_entity_summary("Ninguém")
        assert "Não encontrei" in out
    finally:
        db.close()


def test_format_entity_list_empty(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        out = mgr.format_entity_list()
        assert "Nenhuma entidade" in out
    finally:
        db.close()


def test_format_entity_list_with_entities(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.update_context(name="João", context_snippet="x")
        mgr.update_context(name="Maria", context_snippet="y")
        out = mgr.format_entity_list()
        assert "João" in out
        assert "Maria" in out
        assert "Entidades" in out
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Snippet size cap
# ---------------------------------------------------------------------------

def test_long_snippet_is_truncated(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        long_text = "x" * 5000
        mgr.update_context(name="Foo", context_snippet=long_text)
        rec = mgr.get_entity("Foo")
        snippets = json.loads(rec["context_snippets_json"])
        assert len(snippets[0]["text"]) == 1000  # capped at 1000
    finally:
        db.close()
