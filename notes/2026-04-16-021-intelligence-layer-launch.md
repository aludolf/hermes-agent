# 021 Hermes Intelligence Layer — launch notes

**Date**: 2026-04-16
**Branch**: `feature/021-intelligence-layer` → merged to `deploy`
**Scope**: 6 user stories + 14-task polish phase. See
`specs/021-hermes-intelligence-layer/` in the empty-belt workspace for
the full speckit artifacts.

## What shipped

- **US1**: voice note → Sonnet extraction → action router → pt-BR summary
- **US2**: Word/Excel/PPT/PDF upload → MarkItDown → same pipeline
- **US3**: long transcript → preview → confirm/partial/cancel
- **US4**: Hermes-scoped entity context (`/entity`, `/entities`)
- **US5**: Rauru harness runner (`/run_test`, `/scenarios`, `/test_history`)
- **US6**: 8 shipped YAML scenarios, auto-loaded at gateway startup

Schema bumped to v9 (6 new tables: extraction_events, pending_previews,
entity_context, harness_scenarios_cache, harness_runs,
transcript_extraction_links).

## Key commits

```
feat(021): US6 — scenario YAML loader + 8 shipped Rauru scenarios
feat(021): US5 — Rauru harness runner + regression detector
feat(021): US4 — Hermes-scoped entity context accumulation
feat(021): US3 — long transcript preview + confirmation flow
feat(021): US2 — document upload extraction via MarkItDown
feat(021): intelligence layer MVP — voice note extraction pipeline
```

## VPS verification (after deploy)

```bash
# Imports
ssh personalos-vps "docker exec hermes-gateway /opt/hermes/.venv/bin/python \
  -c 'from agent.orchestrator import (
    extract_actions, DocumentConverter, HarnessRunner,
    EntityContextManager, PendingPreviewManager, ScenarioLoader
  ); print(\"021 imports OK\")'"

# Test suite (021 only)
ssh personalos-vps "docker exec hermes-gateway /opt/hermes/.venv/bin/python \
  -m pytest \
    tests/agent/test_hermes_state_v9_schema.py \
    tests/agent/test_orchestrator_extraction.py \
    tests/agent/test_orchestrator_action_router.py \
    tests/agent/test_orchestrator_document_converter.py \
    tests/agent/test_orchestrator_pending_preview.py \
    tests/agent/test_orchestrator_entity_context.py \
    tests/agent/test_orchestrator_regression_detector.py \
    tests/agent/test_orchestrator_harness_runner.py \
    tests/agent/test_orchestrator_scenario_loader.py \
    tests/agent/test_intelligence_e2e.py \
    tests/gateway/test_intelligence_commands.py \
    --tb=line -q -o 'addopts='"

# Scenario cache populated
ssh personalos-vps "docker exec hermes-gateway /opt/hermes/.venv/bin/python \
  -c 'from hermes_state import SessionDB; db=SessionDB(); \
      rows=db.list_scenarios(); print(f\"scenarios loaded: {len(rows)}\"); \
      [print(f\" - {r[\\\"scenario_id\\\"]}\") for r in rows]'"
```

## Live smoke tests (manual, from Telegram)

### 1. Voice note

Send Hermes a voice note: *"Comprar café amanhã. Reunião com João sexta
às 10h."*

**Expected**:
- Reply in pt-BR summarizing 1 task + 1 meeting.
- "café" appears in `/list Compras`.
- Calendar event created for sexta 10:00.

### 2. Document upload

Upload a `.docx` containing bullet-point action items.

**Expected**: pt-BR summary + items appear in the correct list within 30s.

### 3. Long transcript preview

Paste `/transcript <3+ min meeting transcript>`.

**Expected**:
- Numbered preview message (no execution yet).
- Reply `confirmar 1, 3` → only items 1 and 3 execute.
- 10-min silence → preview auto-expires.

### 4. Harness run

Send `/run_test rauru_smoke` to Hermes.

**Expected**:
- Immediate "Executando rauru_smoke..." reply.
- Within 3 min: per-step pass/fail summary.
- New line in `/opt/hermes/data/harness/results/harness_results.jsonl`.
- New row at `http://129.121.51.4:8090/dashboard/testers/harness` (HD dashboard).

### 5. Entity context

After 2+ voice notes mentioning "João":
- `/entity João` → accumulated context list.
- `/entities` → list of all tracked entities with mention counts.

## Rollback

Extraction can be disabled in-place without a revert:

```bash
ssh personalos-vps "cd /opt/hermes && \
  echo 'HERMES_EXTRACTION_ENABLED=0' >> data/.env && \
  docker compose restart hermes-gateway"
```

With the flag set to `0`, voice notes and documents flow through the
pre-021 LLM session path. Re-enable by removing the line and restarting.

## Known limitations

- Transcript >3min detection currently uses the character-count proxy
  (2000 chars). Future: use audio-duration metadata from the Telegram
  adapter when available.
- Entity collision handling only surfaces via `detect_collision()` — the
  UX prompt is not yet wired. Caller should ask a clarifying question
  before silently merging; this is left to the extraction engine's
  `entity_update` action type for v2.
- Harness scenarios target Telegram only. If Rauru moves to another
  platform, `HarnessRunner.send_message` will need a multi-adapter shim.
