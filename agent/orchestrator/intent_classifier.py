"""LLM-based intent classifier for contact messages (003).

Uses Claude Haiku for cheap, fast classification of natural Portuguese
messages into structured intents. Fires ONLY when regex patterns miss —
the regex fast-path handles the common cases at zero cost.

Intents:
- add_item: contact wants to add something to a list
- complete_item: contact says they bought/did something
- show_list: contact wants to see list contents
- greeting: hi, thanks, bye — social, no action
- question: asking something (weather, time, etc.)
- out_of_scope: request that's outside contact capabilities
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
CLASSIFIER_MAX_TOKENS = 200

CLASSIFIER_SYSTEM_PROMPT = """Você é um classificador de intenções para um assistente doméstico.
Analise a mensagem e retorne APENAS um JSON com a classificação.

Intents possíveis:
- add_item: quer adicionar algo a uma lista (compras, materiais, reparos, recados)
- complete_item: diz que já comprou/fez/concluiu algo
- show_list: quer ver o conteúdo de uma lista
- greeting: cumprimento, agradecimento, despedida (oi, obrigado, tchau, tudo bem)
- question: pergunta sobre algo (clima, hora, etc.)
- out_of_scope: pedido fora do escopo (agenda, email, configurações)

Responda SOMENTE com JSON válido neste formato:
{"intent": "add_item", "items": ["shampoo", "condicionador"], "list": "Compras", "confidence": 0.95}

Para greeting/question/out_of_scope:
{"intent": "greeting", "confidence": 0.99}

Regras:
- Se a pessoa menciona produtos/itens, é SEMPRE add_item (lista padrão: Compras)
- Se menciona material escolar, list = "Material Escolar"
- Se menciona conserto/reparo, list = "Reparos"
- Se menciona tarefa/recado, list = "Recados"
- "já comprei X", "já fiz X", "pronto o X" = complete_item
- Itens podem ser separados por "e", vírgula, ou em linhas separadas"""


@dataclass
class ClassifiedIntent:
    """Structured result from the intent classifier."""
    intent: str
    items: list[str]
    target_list: str | None
    confidence: float
    raw: dict[str, Any]

    @property
    def is_actionable(self) -> bool:
        return self.intent in ("add_item", "complete_item", "show_list")


def classify_contact_message(text: str) -> ClassifiedIntent | None:
    """Classify a contact message using Haiku.

    Returns ClassifiedIntent or None if classification fails.
    Costs ~$0.001 per call. Timeout: 10s.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — intent classifier disabled")
        return None

    try:
        import httpx as _httpx
    except ImportError:
        logger.warning("httpx not available — intent classifier disabled")
        return None

    try:
        response = _httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLASSIFIER_MODEL,
                "max_tokens": CLASSIFIER_MAX_TOKENS,
                "system": CLASSIFIER_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": text}],
            },
            timeout=10.0,
        )
        response.raise_for_status()
    except Exception as e:
        logger.warning("Intent classifier API call failed: %s", e)
        return None

    try:
        data = response.json()
        content_text = data.get("content", [{}])[0].get("text", "")
        # Parse JSON from the response (handle markdown code blocks)
        json_str = content_text.strip()
        if json_str.startswith("```"):
            json_str = json_str.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        parsed = json.loads(json_str)
    except (json.JSONDecodeError, IndexError, KeyError) as e:
        logger.warning("Intent classifier parse failed: %s — raw: %s", e, content_text[:200])
        return None

    intent = parsed.get("intent", "out_of_scope")
    items = parsed.get("items") or []
    if isinstance(items, str):
        items = [items]
    target_list = parsed.get("list")
    confidence = float(parsed.get("confidence", 0.5))

    return ClassifiedIntent(
        intent=intent,
        items=items,
        target_list=target_list,
        confidence=confidence,
        raw=parsed,
    )


# ---------------------------------------------------------------------------
# Test-trigger NL classifier (021 US5, FR-021)
# ---------------------------------------------------------------------------

import re as _re

# "roda o smoke test do rauru" / "executa o rauru_smoke" / "teste de onboarding"
_TEST_TRIGGER_PATTERNS = [
    _re.compile(r"\b(roda|rodar|executa|executar|testa|testar|run)\b", _re.IGNORECASE),
    _re.compile(r"\b(smoke|teste|test|harness|scenario|cenário)\b", _re.IGNORECASE),
]


@dataclass
class TestTriggerIntent:
    """NL request to kick off a harness scenario."""
    is_test_trigger: bool
    scenario_hints: list[str]  # substrings to fuzzy-match against scenario IDs


def classify_test_trigger(
    text: str, *, known_scenario_ids: list[str] | None = None,
) -> TestTriggerIntent:
    """Quick NL matcher for owner messages like "roda o smoke test do rauru".

    Two-of-two match on the trigger verb + test noun patterns classifies as
    a test trigger. `scenario_hints` extracts candidate scenario-id tokens
    (anything that looks like `rauru_smoke`, `smoke`, `onboarding`, etc.)
    so the gateway can fuzzy-match against the loaded scenario cache.
    """
    if not text or len(text) > 300:
        return TestTriggerIntent(is_test_trigger=False, scenario_hints=[])

    verb_hit = bool(_TEST_TRIGGER_PATTERNS[0].search(text))
    noun_hit = bool(_TEST_TRIGGER_PATTERNS[1].search(text))
    if not (verb_hit and noun_hit):
        return TestTriggerIntent(is_test_trigger=False, scenario_hints=[])

    # Tokenize on whitespace + punctuation; preserve underscores so IDs like
    # `rauru_smoke` survive.
    tokens = [t.lower() for t in _re.findall(r"[\w_]+", text) if len(t) >= 3]
    stopwords = {"roda", "rodar", "executa", "executar", "testa", "testar",
                 "test", "smoke", "run", "teste", "harness", "the", "bot",
                 "do", "de", "da", "pro", "com", "por", "and", "scenario",
                 "cenário", "cenario", "um", "uma"}
    hints = [t for t in tokens if t not in stopwords]
    # If we know the available scenario IDs, keep only tokens that substring-match.
    if known_scenario_ids:
        hits = []
        for sid in known_scenario_ids:
            if any(tok in sid.lower() for tok in hints):
                hits.append(sid)
        if hits:
            return TestTriggerIntent(is_test_trigger=True, scenario_hints=hits)
    return TestTriggerIntent(is_test_trigger=True, scenario_hints=hints)
