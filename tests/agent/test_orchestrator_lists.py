"""Tests for the ListManager (003 — productivity orchestrator)."""

from agent.orchestrator.lists import DEFAULT_LISTS, ListManager
from agent.orchestrator.models import ListType
from hermes_state import SessionDB


def _mgr(tmp_path) -> tuple[SessionDB, ListManager]:
    db = SessionDB(db_path=tmp_path / "state.db")
    return db, ListManager(db)


# ---------------------------------------------------------------------------
# List CRUD
# ---------------------------------------------------------------------------

def test_create_list(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        rec = mgr.create_list(name="Compras", list_type="shopping", created_by="owner")
        assert rec["name"] == "Compras"
        assert rec["list_type"] == "shopping"
        assert rec["status"] == "active"
        assert rec["list_id"].startswith("list_")
    finally:
        db.close()


def test_find_list_by_name_case_insensitive(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.create_list(name="Compras", list_type="shopping", created_by="owner")
        assert mgr.find_list_by_name("compras") is not None
        assert mgr.find_list_by_name("COMPRAS") is not None
        assert mgr.find_list_by_name("Compras") is not None
        assert mgr.find_list_by_name("NotFound") is None
    finally:
        db.close()


def test_list_all_active_only_by_default(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        a = mgr.create_list(name="A", list_type="shopping", created_by="owner")
        mgr.create_list(name="B", list_type="custom", created_by="owner")
        mgr.archive_list(a["list_id"])

        active = mgr.list_all(status="active")
        assert len(active) == 1
        assert active[0]["name"] == "B"
    finally:
        db.close()


def test_seed_default_lists_creates_four(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        created = mgr.seed_default_lists(created_by="system")
        assert len(created) == len(DEFAULT_LISTS)
        names = {rec["name"] for rec in created}
        assert names == {name for name, _ in DEFAULT_LISTS}
    finally:
        db.close()


def test_seed_default_lists_idempotent(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.seed_default_lists()
        second = mgr.seed_default_lists()
        assert second == []  # nothing new created

        all_lists = mgr.list_all()
        assert len(all_lists) == len(DEFAULT_LISTS)
    finally:
        db.close()


def test_archive_removes_from_active_listing(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        rec = mgr.create_list(name="Temp", list_type="custom", created_by="owner")
        assert len(mgr.list_all()) == 1

        mgr.archive_list(rec["list_id"])
        assert len(mgr.list_all()) == 0
        assert len(mgr.list_all(status="archived")) == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

def test_add_item_captures_attribution(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        list_rec = mgr.create_list(name="Compras", list_type="shopping", created_by="owner")
        item = mgr.add_item(
            list_rec["list_id"],
            content="alvejante",
            added_by="111222333",
            added_by_name="Maria",
        )
        assert item["content"] == "alvejante"
        assert item["added_by"] == "111222333"
        assert item["added_by_name"] == "Maria"
        assert item["checked"] == 0
    finally:
        db.close()


def test_get_items_excludes_checked_by_default(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        list_rec = mgr.create_list(name="Compras", list_type="shopping", created_by="owner")
        list_id = list_rec["list_id"]

        a = mgr.add_item(list_id, "arroz", added_by="owner")
        mgr.add_item(list_id, "feijão", added_by="owner")
        mgr.check_item(a["item_id"], checked_by="owner")

        unchecked = mgr.get_items(list_id)
        assert len(unchecked) == 1
        assert unchecked[0]["content"] == "feijão"

        all_items = mgr.get_items(list_id, include_checked=True)
        assert len(all_items) == 2
    finally:
        db.close()


def test_check_then_uncheck_item(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        list_rec = mgr.create_list(name="Compras", list_type="shopping", created_by="owner")
        item = mgr.add_item(list_rec["list_id"], "leite", added_by="owner")

        mgr.check_item(item["item_id"], checked_by="owner")
        unchecked = mgr.get_items(list_rec["list_id"])
        assert len(unchecked) == 0

        mgr.uncheck_item(item["item_id"])
        unchecked = mgr.get_items(list_rec["list_id"])
        assert len(unchecked) == 1
    finally:
        db.close()


def test_remove_item(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        list_rec = mgr.create_list(name="Compras", list_type="shopping", created_by="owner")
        item = mgr.add_item(list_rec["list_id"], "pão", added_by="owner")

        mgr.remove_item(item["item_id"])
        assert len(mgr.get_items(list_rec["list_id"], include_checked=True)) == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Multi-user attribution
# ---------------------------------------------------------------------------

def test_multi_user_contributions_preserved(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        list_rec = mgr.create_list(name="Compras", list_type="shopping", created_by="owner")
        list_id = list_rec["list_id"]

        mgr.add_item(list_id, "alvejante", added_by="maid", added_by_name="Maria")
        mgr.add_item(list_id, "pilhas AA", added_by="kid", added_by_name="Pedro")
        mgr.add_item(list_id, "café", added_by="owner", added_by_name="Alexandre")

        items = mgr.get_items(list_id)
        by_who = {item["added_by_name"]: item["content"] for item in items}
        assert by_who == {
            "Maria": "alvejante",
            "Pedro": "pilhas AA",
            "Alexandre": "café",
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def test_format_list_summary_empty(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        rec = mgr.create_list(name="Compras", list_type="shopping", created_by="owner")
        out = mgr.format_list_summary(rec["list_id"])
        assert "Compras" in out
        assert "0 itens" in out
        assert "vazia" in out.lower()
    finally:
        db.close()


def test_format_list_summary_with_items(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        rec = mgr.create_list(name="Compras", list_type="shopping", created_by="owner")
        mgr.add_item(rec["list_id"], "alvejante", added_by="maid", added_by_name="Maria")
        mgr.add_item(rec["list_id"], "café", added_by="owner", added_by_name="Alexandre")

        out = mgr.format_list_summary(rec["list_id"])
        assert "Compras" in out
        assert "2 itens" in out
        assert "alvejante" in out
        assert "Maria" in out
        assert "café" in out
        assert "Alexandre" in out
    finally:
        db.close()


def test_format_all_lists_summary(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.seed_default_lists()
        compras = mgr.find_list_by_name("Compras")
        mgr.add_item(compras["list_id"], "ovos", added_by="owner", added_by_name="Alexandre")

        out = mgr.format_all_lists_summary()
        assert "Compras" in out
        assert "Material Escolar" in out
        assert "Reparos" in out
        assert "Recados" in out
        assert "ovos" in out
    finally:
        db.close()


def test_list_type_enum_values():
    assert ListType.SHOPPING == "shopping"
    assert ListType.SCHOOL == "school"
    assert ListType.REPAIRS == "repairs"
    assert ListType.ERRANDS == "errands"
    assert ListType.CUSTOM == "custom"
