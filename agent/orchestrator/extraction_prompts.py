"""Extraction prompt builders for the Sonnet-powered extraction engine (021).

Portuguese (pt-BR) system prompt with cultural context. See
specs/021-hermes-intelligence-layer/contracts/extraction-contract.md for the
stable contract that governs the shape of the input/output here.
"""

from __future__ import annotations

from datetime import date, timedelta


SYSTEM_PROMPT_TEMPLATE = """Você é um extrator de ações de transcrições de áudio e documentos.

Analise o texto e extraia TODAS as ações mencionadas. Retorne JSON.

Tipos de ação:
- "task": comprar, fazer, ou entregar algo. Campos em metadata: list (Compras|Material Escolar|Reparos|Recados), assignee (opcional)
- "meeting": reunião ou compromisso com data/hora. Campos em metadata: summary, date (YYYY-MM-DD), time (HH:MM), location (opcional), attendees (lista, opcional)
- "reminder": lembrete com data/hora. Campos em metadata: title, date (YYYY-MM-DD), time (HH:MM)
- "kb_entry": informação valiosa para base de conhecimento. Campos em metadata: title, content, domain (opcional)
- "entity_update": atualização sobre uma pessoa ou projeto. Campos em metadata: entity, context
- "decision": decisão tomada em reunião. Campos em metadata: content, context (opcional)
- "info": informação geral sem ação necessária. Campos em metadata: content

Regras de confiança (campo "confidence", 0.0 a 1.0):
- Ação explícita e clara com data/hora → 0.85-0.99
- Ação clara mas com alguma ambiguidade → 0.70-0.84 (vai pedir confirmação)
- Menção vaga ou hipotética → abaixo de 0.70 (vai ser ignorado)

Datas relativas (timezone America/Sao_Paulo):
- "hoje" = {current_date}
- "amanhã" = {tomorrow}
- "sexta", "segunda" etc. = próxima ocorrência do dia da semana
- Horas sem AM/PM em português assumem 24h

Listas disponíveis: {active_lists}

Responda SOMENTE com JSON válido, sem markdown. Formato:
{{"actions": [{{...}}], "entities_mentioned": [...], "summary": "..."}}"""


def build_extraction_system_prompt(
    active_lists: list[str],
    current_date: date | None = None,
) -> str:
    """Render the Portuguese extraction system prompt.

    Args:
        active_lists: names of the owner's active shared lists (e.g. ["Compras",
            "Material Escolar", "Reparos", "Recados"]).
        current_date: date to treat as "today". Defaults to today in the host
            timezone — callers should pass an explicit date computed in
            America/Sao_Paulo to keep extractions stable.

    Returns:
        A fully-rendered system prompt string.
    """
    today = current_date or date.today()
    tomorrow = today + timedelta(days=1)
    lists_str = ", ".join(active_lists) if active_lists else "Compras"
    return SYSTEM_PROMPT_TEMPLATE.format(
        current_date=today.isoformat(),
        tomorrow=tomorrow.isoformat(),
        active_lists=lists_str,
    )
