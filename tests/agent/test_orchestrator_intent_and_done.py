"""Tests for intent classifier, fuzzy item match, and /done flow."""

from unittest.mock import patch, MagicMock

from agent.orchestrator.lists import ListManager
from agent.orchestrator.intent_classifier import ClassifiedIntent, classify_contact_message
from hermes_state import SessionDB


# ---------------------------------------------------------------------------
# Fuzzy item matching
# ---------------------------------------------------------------------------

def test_fuzzy_match_exact(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        mgr = ListManager(db)
        rec = mgr.create_list("Compras", "shopping", created_by="owner")
        mgr.add_item(rec["list_id"], "leite", added_by="owner")

        match = mgr.find_item_fuzzy("leite")
        assert match is not None
        assert match["content"] == "leite"
    finally:
        db.close()


def test_fuzzy_match_case_insensitive(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        mgr = ListManager(db)
        rec = mgr.create_list("Compras", "shopping", created_by="owner")
        mgr.add_item(rec["list_id"], "Café Especial", added_by="owner")

        match = mgr.find_item_fuzzy("café especial")
        assert match is not None
        assert match["content"] == "Café Especial"
    finally:
        db.close()


def test_fuzzy_match_substring(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        mgr = ListManager(db)
        rec = mgr.create_list("Compras", "shopping", created_by="owner")
        mgr.add_item(rec["list_id"], "papel higiênico dupla folha", added_by="owner")

        match = mgr.find_item_fuzzy("papel higiênico")
        assert match is not None
        assert "papel" in match["content"]
    finally:
        db.close()


def test_fuzzy_match_across_lists(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        mgr = ListManager(db)
        compras = mgr.create_list("Compras", "shopping", created_by="owner")
        reparos = mgr.create_list("Reparos", "repairs", created_by="owner")
        mgr.add_item(compras["list_id"], "leite", added_by="owner")
        mgr.add_item(reparos["list_id"], "torneira da cozinha", added_by="owner")

        # Find across all lists
        match = mgr.find_item_fuzzy("torneira")
        assert match is not None
        assert "torneira" in match["content"]
    finally:
        db.close()


def test_fuzzy_match_no_match(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        mgr = ListManager(db)
        mgr.create_list("Compras", "shopping", created_by="owner")
        assert mgr.find_item_fuzzy("unicórnio") is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# check_item_by_text
# ---------------------------------------------------------------------------

def test_check_item_by_text_marks_done(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        mgr = ListManager(db)
        rec = mgr.create_list("Compras", "shopping", created_by="owner")
        mgr.add_item(rec["list_id"], "arroz", added_by="owner")

        result = mgr.check_item_by_text("arroz", checked_by="owner")
        assert result is not None
        assert result["content"] == "arroz"
        assert result["list_name"] == "Compras"

        # Should not appear in unchecked
        items = mgr.get_items(rec["list_id"])
        assert len(items) == 0
    finally:
        db.close()


def test_check_item_by_text_returns_none_when_not_found(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        mgr = ListManager(db)
        mgr.create_list("Compras", "shopping", created_by="owner")
        assert mgr.check_item_by_text("inexistente", checked_by="owner") is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ClassifiedIntent dataclass
# ---------------------------------------------------------------------------

def test_classified_intent_actionable():
    ci = ClassifiedIntent(intent="add_item", items=["leite"], target_list="Compras", confidence=0.95, raw={})
    assert ci.is_actionable is True


def test_classified_intent_non_actionable():
    ci = ClassifiedIntent(intent="greeting", items=[], target_list=None, confidence=0.99, raw={})
    assert ci.is_actionable is False


# ---------------------------------------------------------------------------
# Intent classifier (mocked API)
# ---------------------------------------------------------------------------

def _mock_haiku_response(text_json: str):
    """Create a mock httpx module that returns the given JSON text."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"content": [{"text": text_json}]}

    mock_httpx = MagicMock()
    mock_httpx.post.return_value = mock_resp
    return mock_httpx


def test_classifier_parses_add_item_response():
    mock_httpx = _mock_haiku_response(
        '{"intent": "add_item", "items": ["shampoo", "condicionador"], "list": "Compras", "confidence": 0.95}'
    )
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            result = classify_contact_message("shampoo e condicionador")

    assert result is not None
    assert result.intent == "add_item"
    assert result.items == ["shampoo", "condicionador"]
    assert result.target_list == "Compras"
    assert result.confidence == 0.95


def test_classifier_parses_complete_item_response():
    mock_httpx = _mock_haiku_response(
        '{"intent": "complete_item", "items": ["leite"], "confidence": 0.92}'
    )
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            result = classify_contact_message("já comprei o leite")

    assert result is not None
    assert result.intent == "complete_item"
    assert result.items == ["leite"]


def test_classifier_returns_none_without_api_key():
    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
        result = classify_contact_message("anything")
    assert result is None


def test_classifier_handles_api_failure():
    mock_httpx = MagicMock()
    mock_httpx.post.side_effect = Exception("timeout")
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            result = classify_contact_message("something")
    assert result is None
