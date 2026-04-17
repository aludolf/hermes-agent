#!/usr/bin/env python3
"""
SQLite State Store for Hermes Agent.

Provides persistent session storage with FTS5 full-text search, replacing
the per-session JSONL file approach. Stores session metadata, full message
history, and model configuration for CLI and gateway sessions.

Key design decisions:
- WAL mode for concurrent readers + one writer (gateway multi-platform)
- FTS5 virtual table for fast text search across all session messages
- Compression-triggered session splitting via parent_session_id chains
- Batch runner and RL trajectories are NOT stored here (separate systems)
- Session source tagging ('cli', 'telegram', 'discord', etc.) for filtering
"""

import json
import logging
import random
import re
import sqlite3
import threading
import time
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Any, Callable, Dict, List, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_DB_PATH = get_hermes_home() / "state.db"

SCHEMA_VERSION = 10

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


_ORCHESTRATOR_SCHEMA_SQL = """
-- =====================================================================
-- Orchestrator foundation tables (001)
-- =====================================================================

CREATE TABLE IF NOT EXISTS orchestrator_jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    route_class TEXT NOT NULL DEFAULT 'pending_route',
    requested_by TEXT,
    source_type TEXT,
    source_uri TEXT,
    source_scope TEXT,
    intent TEXT,
    priority INTEGER DEFAULT 0,
    metadata_json TEXT,
    error_message TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);

CREATE INDEX IF NOT EXISTS idx_orch_jobs_status ON orchestrator_jobs(status);
CREATE INDEX IF NOT EXISTS idx_orch_jobs_route ON orchestrator_jobs(route_class);
CREATE INDEX IF NOT EXISTS idx_orch_jobs_created ON orchestrator_jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS orchestrator_artifacts (
    artifact_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES orchestrator_jobs(job_id),
    artifact_type TEXT NOT NULL,
    storage_backend TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    mime_type TEXT,
    checksum TEXT,
    size_bytes INTEGER,
    provenance_json TEXT,
    metadata_json TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orch_artifacts_job ON orchestrator_artifacts(job_id);

CREATE TABLE IF NOT EXISTS routing_decisions (
    decision_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES orchestrator_jobs(job_id),
    route_class TEXT NOT NULL,
    decision_reason TEXT,
    confidence REAL DEFAULT 0.0,
    manual_override INTEGER DEFAULT 0,
    overridden_from TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_routing_job ON routing_decisions(job_id);

CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES orchestrator_jobs(job_id),
    action_id TEXT,
    policy_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    resolved_by TEXT,
    resolution_note TEXT,
    created_at REAL NOT NULL,
    resolved_at REAL
);

CREATE INDEX IF NOT EXISTS idx_approvals_job ON approval_requests(job_id);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approval_requests(status);

-- =====================================================================
-- Knowledge layer split tables (002)
-- =====================================================================

CREATE TABLE IF NOT EXISTS layer_assignments (
    assignment_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES orchestrator_jobs(job_id),
    route_class TEXT NOT NULL,
    knowledge_tier TEXT NOT NULL,
    decision_reason TEXT,
    confidence REAL DEFAULT 0.0,
    manual_override INTEGER DEFAULT 0,
    overridden_from_tier TEXT,
    decided_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_layer_job ON layer_assignments(job_id);
CREATE INDEX IF NOT EXISTS idx_layer_tier ON layer_assignments(knowledge_tier);

CREATE TABLE IF NOT EXISTS working_artifacts (
    working_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES orchestrator_jobs(job_id),
    evidence_id TEXT,
    artifact_kind TEXT NOT NULL,
    destination_backend TEXT NOT NULL DEFAULT 'working_repo',
    destination_path TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    metadata_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL
);

CREATE INDEX IF NOT EXISTS idx_working_job ON working_artifacts(job_id);
CREATE INDEX IF NOT EXISTS idx_working_status ON working_artifacts(status);

CREATE TABLE IF NOT EXISTS canonical_candidates (
    candidate_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES orchestrator_jobs(job_id),
    evidence_id TEXT,
    working_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending_review',
    review_summary TEXT,
    metadata_json TEXT,
    created_at REAL NOT NULL,
    reviewed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_candidates_status ON canonical_candidates(status);
CREATE INDEX IF NOT EXISTS idx_candidates_job ON canonical_candidates(job_id);

CREATE TABLE IF NOT EXISTS canonical_publications (
    publication_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES canonical_candidates(candidate_id),
    destination_repo TEXT NOT NULL,
    entry_ids_json TEXT,
    files_changed_json TEXT,
    validation_status TEXT NOT NULL DEFAULT 'pending',
    published_at REAL,
    published_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_publications_candidate ON canonical_publications(candidate_id);

CREATE TABLE IF NOT EXISTS promotion_decisions (
    decision_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES canonical_candidates(candidate_id),
    status TEXT NOT NULL DEFAULT 'pending',
    policy_name TEXT,
    requested_at REAL NOT NULL,
    resolved_at REAL,
    resolved_by TEXT,
    resolution_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_promo_candidate ON promotion_decisions(candidate_id);
CREATE INDEX IF NOT EXISTS idx_promo_status ON promotion_decisions(status);

CREATE TABLE IF NOT EXISTS lineage_records (
    lineage_id TEXT PRIMARY KEY,
    root_evidence_id TEXT NOT NULL,
    working_ids_json TEXT,
    candidate_ids_json TEXT,
    publication_ids_json TEXT,
    last_updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lineage_evidence ON lineage_records(root_evidence_id);

CREATE TABLE IF NOT EXISTS consumption_policies (
    policy_id TEXT PRIMARY KEY,
    consumer_name TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL DEFAULT 'canonical_first',
    fallback_allowed INTEGER DEFAULT 1,
    working_visibility TEXT NOT NULL DEFAULT 'none',
    metadata_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL
);

CREATE INDEX IF NOT EXISTS idx_consumption_consumer ON consumption_policies(consumer_name);

-- =====================================================================
-- Productivity Orchestrator tables (003)
-- =====================================================================

CREATE TABLE IF NOT EXISTS contact_roles (
    contact_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'pending',
    language TEXT NOT NULL DEFAULT 'pt-BR',
    approved_capabilities TEXT,
    approved_by TEXT,
    approved_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL
);

CREATE INDEX IF NOT EXISTS idx_contacts_role ON contact_roles(role);

-- Shared lists (shopping, school, repairs, errands, custom)
CREATE TABLE IF NOT EXISTS shared_lists (
    list_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    list_type TEXT NOT NULL DEFAULT 'shopping',
    status TEXT NOT NULL DEFAULT 'active',
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL
);

CREATE INDEX IF NOT EXISTS idx_lists_status ON shared_lists(status);
CREATE INDEX IF NOT EXISTS idx_lists_type ON shared_lists(list_type);

CREATE TABLE IF NOT EXISTS list_items (
    item_id TEXT PRIMARY KEY,
    list_id TEXT NOT NULL REFERENCES shared_lists(list_id),
    content TEXT NOT NULL,
    checked INTEGER DEFAULT 0,
    added_by TEXT NOT NULL,
    added_by_name TEXT,
    checked_by TEXT,
    created_at REAL NOT NULL,
    checked_at REAL
);

CREATE INDEX IF NOT EXISTS idx_items_list ON list_items(list_id);
CREATE INDEX IF NOT EXISTS idx_items_checked ON list_items(checked);

-- Reminders with Google Calendar sync
CREATE TABLE IF NOT EXISTS reminders (
    reminder_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    title TEXT NOT NULL,
    due_at REAL NOT NULL,
    google_event_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    completed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_reminders_status ON reminders(status);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(due_at);

-- Meeting reminder dedup (prevent duplicate cron notifications)
CREATE TABLE IF NOT EXISTS meeting_reminders_sent (
    event_id TEXT PRIMARY KEY,
    reminded_at REAL NOT NULL
);

-- =====================================================================
-- Intelligence Layer tables (021)
-- =====================================================================

CREATE TABLE IF NOT EXISTS extraction_events (
    extraction_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_format TEXT,
    source_job_id TEXT,
    transcript_hash TEXT NOT NULL,
    transcript_preview TEXT,
    transcript_full_path TEXT,
    sender_id TEXT NOT NULL,
    sender_role TEXT NOT NULL,
    actions_json TEXT,
    actions_count INTEGER DEFAULT 0,
    execution_mode TEXT NOT NULL,
    language TEXT DEFAULT 'pt-BR',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_extraction_hash ON extraction_events(transcript_hash);
CREATE INDEX IF NOT EXISTS idx_extraction_sender ON extraction_events(sender_id);
CREATE INDEX IF NOT EXISTS idx_extraction_created ON extraction_events(created_at DESC);

CREATE TABLE IF NOT EXISTS pending_previews (
    preview_id TEXT PRIMARY KEY,
    extraction_id TEXT NOT NULL REFERENCES extraction_events(extraction_id),
    sender_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    actions_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'awaiting_confirmation',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    resolved_at REAL
);

CREATE INDEX IF NOT EXISTS idx_preview_sender ON pending_previews(sender_id);
CREATE INDEX IF NOT EXISTS idx_preview_status ON pending_previews(status);
CREATE INDEX IF NOT EXISTS idx_preview_expires ON pending_previews(expires_at);

CREATE TABLE IF NOT EXISTS entity_context (
    entity_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    name_lower TEXT NOT NULL UNIQUE,
    entity_type TEXT NOT NULL DEFAULT 'person',
    context_snippets_json TEXT,
    first_mentioned_at REAL NOT NULL,
    last_mentioned_at REAL NOT NULL,
    mention_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL
);

CREATE INDEX IF NOT EXISTS idx_entity_type ON entity_context(entity_type);
CREATE INDEX IF NOT EXISTS idx_entity_last_mentioned ON entity_context(last_mentioned_at DESC);

CREATE TABLE IF NOT EXISTS harness_scenarios_cache (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT,
    preconditions_json TEXT,
    steps_json TEXT NOT NULL,
    estimated_duration_ms INTEGER,
    target_bot TEXT NOT NULL,
    target_chat_id TEXT NOT NULL,
    yaml_path TEXT NOT NULL,
    yaml_hash TEXT NOT NULL,
    loaded_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scenarios_category ON harness_scenarios_cache(category);

CREATE TABLE IF NOT EXISTS harness_runs (
    run_id TEXT PRIMARY KEY,
    scenario_id TEXT NOT NULL,
    triggered_by TEXT NOT NULL,
    trigger_source TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    duration_ms INTEGER,
    status TEXT NOT NULL,
    steps_json TEXT,
    regression_flags_json TEXT,
    last_passing_run_id TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_scenario ON harness_runs(scenario_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON harness_runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_started ON harness_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS transcript_extraction_links (
    link_id TEXT PRIMARY KEY,
    extraction_id TEXT NOT NULL REFERENCES extraction_events(extraction_id),
    job_id TEXT NOT NULL REFERENCES orchestrator_jobs(job_id),
    working_artifact_id TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_links_extraction ON transcript_extraction_links(extraction_id);
CREATE INDEX IF NOT EXISTS idx_links_job ON transcript_extraction_links(job_id);

-- =====================================================================
-- Teams & Email Sentinels tables (022)
-- =====================================================================

CREATE TABLE IF NOT EXISTS credentials_store (
    credential_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    ciphertext BLOB NOT NULL,
    label TEXT,
    created_at REAL NOT NULL,
    rotated_at REAL
);

CREATE INDEX IF NOT EXISTS idx_credentials_kind ON credentials_store(kind);

CREATE TABLE IF NOT EXISTS teams_watches (
    watch_id TEXT PRIMARY KEY,
    alias TEXT,
    resource_type TEXT NOT NULL,
    ms_resource_id TEXT NOT NULL,
    subscription_id TEXT,
    subscription_expires_at REAL,
    client_state TEXT,
    delivery_mode TEXT NOT NULL DEFAULT 'realtime',
    polling_cursor TEXT,
    mute_until REAL,
    quiet_hours_start INTEGER,
    quiet_hours_end INTEGER,
    last_event_at REAL,
    owner_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL,
    UNIQUE(owner_id, ms_resource_id)
);

CREATE INDEX IF NOT EXISTS idx_teams_watches_owner ON teams_watches(owner_id);
CREATE INDEX IF NOT EXISTS idx_teams_watches_expires ON teams_watches(subscription_expires_at);
CREATE INDEX IF NOT EXISTS idx_teams_watches_alias ON teams_watches(owner_id, alias);

CREATE TABLE IF NOT EXISTS mail_accounts (
    account_id TEXT PRIMARY KEY,
    alias TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 993,
    username TEXT NOT NULL,
    auth_method TEXT NOT NULL,
    credential_ref TEXT NOT NULL REFERENCES credentials_store(credential_id),
    connection_state TEXT NOT NULL DEFAULT 'disconnected',
    last_error TEXT,
    last_successful_sync_at REAL,
    owner_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL,
    UNIQUE(owner_id, alias)
);

CREATE INDEX IF NOT EXISTS idx_mail_accounts_owner ON mail_accounts(owner_id);
CREATE INDEX IF NOT EXISTS idx_mail_accounts_state ON mail_accounts(connection_state);

CREATE TABLE IF NOT EXISTS mail_watches (
    watch_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES mail_accounts(account_id),
    folder TEXT NOT NULL,
    last_seen_uid INTEGER,
    uidvalidity INTEGER,
    idle_state TEXT NOT NULL DEFAULT 'disconnected',
    last_activity_at REAL,
    reconnect_attempts INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE(account_id, folder)
);

CREATE INDEX IF NOT EXISTS idx_mail_watches_account ON mail_watches(account_id);
CREATE INDEX IF NOT EXISTS idx_mail_watches_state ON mail_watches(idle_state);

CREATE TABLE IF NOT EXISTS backfill_jobs (
    job_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES mail_accounts(account_id),
    scope_hash TEXT NOT NULL,
    folders_json TEXT NOT NULL,
    since_ts REAL,
    until_ts REAL,
    total_estimated INTEGER,
    processed_count INTEGER DEFAULT 0,
    failed_count INTEGER DEFAULT 0,
    rate_limit_msgs_per_min INTEGER DEFAULT 60,
    state TEXT NOT NULL DEFAULT 'queued',
    resumption_cursor_json TEXT,
    error_message TEXT,
    started_at REAL,
    finished_at REAL,
    owner_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_backfill_jobs_account ON backfill_jobs(account_id);
CREATE INDEX IF NOT EXISTS idx_backfill_jobs_state ON backfill_jobs(state);
CREATE INDEX IF NOT EXISTS idx_backfill_jobs_scope ON backfill_jobs(account_id, scope_hash);
"""


class SessionDB:
    """
    SQLite-backed session storage with FTS5 search.

    Thread-safe for the common gateway pattern (multiple reader threads,
    single writer via WAL mode). Each method opens its own cursor.
    """

    # ── Write-contention tuning ──
    # With multiple hermes processes (gateway + CLI sessions + worktree agents)
    # all sharing one state.db, WAL write-lock contention causes visible TUI
    # freezes.  SQLite's built-in busy handler uses a deterministic sleep
    # schedule that causes convoy effects under high concurrency.
    #
    # Instead, we keep the SQLite timeout short (1s) and handle retries at the
    # application level with random jitter, which naturally staggers competing
    # writers and avoids the convoy.
    _WRITE_MAX_RETRIES = 15
    _WRITE_RETRY_MIN_S = 0.020   # 20ms
    _WRITE_RETRY_MAX_S = 0.150   # 150ms
    # Attempt a PASSIVE WAL checkpoint every N successful writes.
    _CHECKPOINT_EVERY_N_WRITES = 50

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._write_count = 0
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            # Short timeout — application-level retry with random jitter
            # handles contention instead of sitting in SQLite's internal
            # busy handler for up to 30s.
            timeout=1.0,
            # Autocommit mode: Python's default isolation_level="" auto-starts
            # transactions on DML, which conflicts with our explicit
            # BEGIN IMMEDIATE.  None = we manage transactions ourselves.
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        self._init_schema()

    # ── Core write helper ──

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Execute a write transaction with BEGIN IMMEDIATE and jitter retry.

        *fn* receives the connection and should perform INSERT/UPDATE/DELETE
        statements.  The caller must NOT call ``commit()`` — that's handled
        here after *fn* returns.

        BEGIN IMMEDIATE acquires the WAL write lock at transaction start
        (not at commit time), so lock contention surfaces immediately.
        On ``database is locked``, we release the Python lock, sleep a
        random 20-150ms, and retry — breaking the convoy pattern that
        SQLite's built-in deterministic backoff creates.

        Returns whatever *fn* returns.
        """
        last_err: Optional[Exception] = None
        for attempt in range(self._WRITE_MAX_RETRIES):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                # Success — periodic best-effort checkpoint.
                self._write_count += 1
                if self._write_count % self._CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                return result
            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                if "locked" in err_msg or "busy" in err_msg:
                    last_err = exc
                    if attempt < self._WRITE_MAX_RETRIES - 1:
                        jitter = random.uniform(
                            self._WRITE_RETRY_MIN_S,
                            self._WRITE_RETRY_MAX_S,
                        )
                        time.sleep(jitter)
                        continue
                # Non-lock error or retries exhausted — propagate.
                raise
        # Retries exhausted (shouldn't normally reach here).
        raise last_err or sqlite3.OperationalError(
            "database is locked after max retries"
        )

    def _try_wal_checkpoint(self) -> None:
        """Best-effort PASSIVE WAL checkpoint.  Never blocks, never raises.

        Flushes committed WAL frames back into the main DB file for any
        frames that no other connection currently needs.  Keeps the WAL
        from growing unbounded when many processes hold persistent
        connections.
        """
        try:
            with self._lock:
                result = self._conn.execute(
                    "PRAGMA wal_checkpoint(PASSIVE)"
                ).fetchone()
                if result and result[1] > 0:
                    logger.debug(
                        "WAL checkpoint: %d/%d pages checkpointed",
                        result[2], result[1],
                    )
        except Exception:
            pass  # Best effort — never fatal.

    def close(self):
        """Close the database connection.

        Attempts a PASSIVE WAL checkpoint first so that exiting processes
        help keep the WAL file from growing unbounded.
        """
        with self._lock:
            if self._conn:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except Exception:
                    pass
                self._conn.close()
                self._conn = None

    def _init_schema(self):
        """Create tables and FTS if they don't exist, run migrations."""
        cursor = self._conn.cursor()

        cursor.executescript(SCHEMA_SQL)

        # Check schema version and run migrations
        cursor.execute("SELECT version FROM schema_version LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            cursor.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        else:
            current_version = row["version"] if isinstance(row, sqlite3.Row) else row[0]
            if current_version < 2:
                # v2: add finish_reason column to messages
                try:
                    cursor.execute("ALTER TABLE messages ADD COLUMN finish_reason TEXT")
                except sqlite3.OperationalError:
                    pass  # Column already exists
                cursor.execute("UPDATE schema_version SET version = 2")
            if current_version < 3:
                # v3: add title column to sessions
                try:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
                except sqlite3.OperationalError:
                    pass  # Column already exists
                cursor.execute("UPDATE schema_version SET version = 3")
            if current_version < 4:
                # v4: add unique index on title (NULLs allowed, only non-NULL must be unique)
                try:
                    cursor.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique "
                        "ON sessions(title) WHERE title IS NOT NULL"
                    )
                except sqlite3.OperationalError:
                    pass  # Index already exists
                cursor.execute("UPDATE schema_version SET version = 4")
            if current_version < 5:
                new_columns = [
                    ("cache_read_tokens", "INTEGER DEFAULT 0"),
                    ("cache_write_tokens", "INTEGER DEFAULT 0"),
                    ("reasoning_tokens", "INTEGER DEFAULT 0"),
                    ("billing_provider", "TEXT"),
                    ("billing_base_url", "TEXT"),
                    ("billing_mode", "TEXT"),
                    ("estimated_cost_usd", "REAL"),
                    ("actual_cost_usd", "REAL"),
                    ("cost_status", "TEXT"),
                    ("cost_source", "TEXT"),
                    ("pricing_version", "TEXT"),
                ]
                for name, column_type in new_columns:
                    try:
                        # name and column_type come from the hardcoded tuple above,
                        # not user input. Double-quote identifier escaping is applied
                        # as defense-in-depth; SQLite DDL cannot be parameterized.
                        safe_name = name.replace('"', '""')
                        cursor.execute(f'ALTER TABLE sessions ADD COLUMN "{safe_name}" {column_type}')
                    except sqlite3.OperationalError:
                        pass
                cursor.execute("UPDATE schema_version SET version = 5")
            if current_version < 6:
                # v6: add reasoning columns to messages table — preserves assistant
                # reasoning text and structured reasoning_details across gateway
                # session turns.  Without these, reasoning chains are lost on
                # session reload, breaking multi-turn reasoning continuity for
                # providers that replay reasoning (OpenRouter, OpenAI, Nous).
                for col_name, col_type in [
                    ("reasoning", "TEXT"),
                    ("reasoning_details", "TEXT"),
                    ("codex_reasoning_items", "TEXT"),
                ]:
                    try:
                        safe = col_name.replace('"', '""')
                        cursor.execute(
                            f'ALTER TABLE messages ADD COLUMN "{safe}" {col_type}'
                        )
                    except sqlite3.OperationalError:
                        pass  # Column already exists
                cursor.execute("UPDATE schema_version SET version = 6")
            if current_version < 7:
                # v7: orchestrator tables + knowledge layer split
                cursor.executescript(_ORCHESTRATOR_SCHEMA_SQL)
                cursor.execute("UPDATE schema_version SET version = 7")
            if current_version < 8:
                # v8: productivity orchestrator (contacts + future lists/reminders)
                cursor.executescript(_ORCHESTRATOR_SCHEMA_SQL)
                cursor.execute("UPDATE schema_version SET version = 8")
            if current_version < 9:
                # v9: intelligence layer (021) — extraction events, pending previews,
                # entity context, harness scenarios + runs, transcript-extraction lineage
                cursor.executescript(_ORCHESTRATOR_SCHEMA_SQL)
                cursor.execute("UPDATE schema_version SET version = 9")
            if current_version < 10:
                # v10: teams + email sentinels (022) — credentials_store, teams_watches,
                # mail_accounts, mail_watches, backfill_jobs
                cursor.executescript(_ORCHESTRATOR_SCHEMA_SQL)
                cursor.execute("UPDATE schema_version SET version = 10")

        # Ensure orchestrator tables exist for fresh databases too
        cursor.executescript(_ORCHESTRATOR_SCHEMA_SQL)

        # Unique title index — always ensure it exists (safe to run after migrations
        # since the title column is guaranteed to exist at this point)
        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique "
                "ON sessions(title) WHERE title IS NOT NULL"
            )
        except sqlite3.OperationalError:
            pass  # Index already exists

        # FTS5 setup (separate because CREATE VIRTUAL TABLE can't be in executescript with IF NOT EXISTS reliably)
        try:
            cursor.execute("SELECT * FROM messages_fts LIMIT 0")
        except sqlite3.OperationalError:
            cursor.executescript(FTS_SQL)

        self._conn.commit()

    # =========================================================================
    # Session lifecycle
    # =========================================================================

    def create_session(
        self,
        session_id: str,
        source: str,
        model: str = None,
        model_config: Dict[str, Any] = None,
        system_prompt: str = None,
        user_id: str = None,
        parent_session_id: str = None,
    ) -> str:
        """Create a new session record. Returns the session_id."""
        def _do(conn):
            conn.execute(
                """INSERT OR IGNORE INTO sessions (id, source, user_id, model, model_config,
                   system_prompt, parent_session_id, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    source,
                    user_id,
                    model,
                    json.dumps(model_config) if model_config else None,
                    system_prompt,
                    parent_session_id,
                    time.time(),
                ),
            )
        self._execute_write(_do)
        return session_id

    def end_session(self, session_id: str, end_reason: str) -> None:
        """Mark a session as ended."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
                (time.time(), end_reason, session_id),
            )
        self._execute_write(_do)

    def reopen_session(self, session_id: str) -> None:
        """Clear ended_at/end_reason so a session can be resumed."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?",
                (session_id,),
            )
        self._execute_write(_do)

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        """Store the full assembled system prompt snapshot."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET system_prompt = ? WHERE id = ?",
                (system_prompt, session_id),
            )
        self._execute_write(_do)

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: Optional[float] = None,
        actual_cost_usd: Optional[float] = None,
        cost_status: Optional[str] = None,
        cost_source: Optional[str] = None,
        pricing_version: Optional[str] = None,
        billing_provider: Optional[str] = None,
        billing_base_url: Optional[str] = None,
        billing_mode: Optional[str] = None,
        absolute: bool = False,
    ) -> None:
        """Update token counters and backfill model if not already set.

        When *absolute* is False (default), values are **incremented** — use
        this for per-API-call deltas (CLI path).

        When *absolute* is True, values are **set directly** — use this when
        the caller already holds cumulative totals (gateway path, where the
        cached agent accumulates across messages).
        """
        if absolute:
            sql = """UPDATE sessions SET
                   input_tokens = ?,
                   output_tokens = ?,
                   cache_read_tokens = ?,
                   cache_write_tokens = ?,
                   reasoning_tokens = ?,
                   estimated_cost_usd = COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?)
                   WHERE id = ?"""
        else:
            sql = """UPDATE sessions SET
                   input_tokens = input_tokens + ?,
                   output_tokens = output_tokens + ?,
                   cache_read_tokens = cache_read_tokens + ?,
                   cache_write_tokens = cache_write_tokens + ?,
                   reasoning_tokens = reasoning_tokens + ?,
                   estimated_cost_usd = COALESCE(estimated_cost_usd, 0) + COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE COALESCE(actual_cost_usd, 0) + ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?)
                   WHERE id = ?"""
        params = (
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
            reasoning_tokens,
            estimated_cost_usd,
            actual_cost_usd,
            actual_cost_usd,
            cost_status,
            cost_source,
            pricing_version,
            billing_provider,
            billing_base_url,
            billing_mode,
            model,
            session_id,
        )
        def _do(conn):
            conn.execute(sql, params)
        self._execute_write(_do)

    def ensure_session(
        self,
        session_id: str,
        source: str = "unknown",
        model: str = None,
    ) -> None:
        """Ensure a session row exists, creating it with minimal metadata if absent.

        Used by _flush_messages_to_session_db to recover from a failed
        create_session() call (e.g. transient SQLite lock at agent startup).
        INSERT OR IGNORE is safe to call even when the row already exists.
        """
        def _do(conn):
            conn.execute(
                """INSERT OR IGNORE INTO sessions
                   (id, source, model, started_at)
                   VALUES (?, ?, ?, ?)""",
                (session_id, source, model, time.time()),
            )
        self._execute_write(_do)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get a session by ID."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def resolve_session_id(self, session_id_or_prefix: str) -> Optional[str]:
        """Resolve an exact or uniquely prefixed session ID to the full ID.

        Returns the exact ID when it exists. Otherwise treats the input as a
        prefix and returns the single matching session ID if the prefix is
        unambiguous. Returns None for no matches or ambiguous prefixes.
        """
        exact = self.get_session(session_id_or_prefix)
        if exact:
            return exact["id"]

        escaped = (
            session_id_or_prefix
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\' ORDER BY started_at DESC LIMIT 2",
                (f"{escaped}%",),
            )
            matches = [row["id"] for row in cursor.fetchall()]
        if len(matches) == 1:
            return matches[0]
        return None

    # Maximum length for session titles
    MAX_TITLE_LENGTH = 100

    @staticmethod
    def sanitize_title(title: Optional[str]) -> Optional[str]:
        """Validate and sanitize a session title.

        - Strips leading/trailing whitespace
        - Removes ASCII control characters (0x00-0x1F, 0x7F) and problematic
          Unicode control chars (zero-width, RTL/LTR overrides, etc.)
        - Collapses internal whitespace runs to single spaces
        - Normalizes empty/whitespace-only strings to None
        - Enforces MAX_TITLE_LENGTH

        Returns the cleaned title string or None.
        Raises ValueError if the title exceeds MAX_TITLE_LENGTH after cleaning.
        """
        if not title:
            return None

        # Remove ASCII control characters (0x00-0x1F, 0x7F) but keep
        # whitespace chars (\t=0x09, \n=0x0A, \r=0x0D) so they can be
        # normalized to spaces by the whitespace collapsing step below
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', title)

        # Remove problematic Unicode control characters:
        # - Zero-width chars (U+200B-U+200F, U+FEFF)
        # - Directional overrides (U+202A-U+202E, U+2066-U+2069)
        # - Object replacement (U+FFFC), interlinear annotation (U+FFF9-U+FFFB)
        cleaned = re.sub(
            r'[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufffc\ufff9-\ufffb]',
            '', cleaned,
        )

        # Collapse internal whitespace runs and strip
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()

        if not cleaned:
            return None

        if len(cleaned) > SessionDB.MAX_TITLE_LENGTH:
            raise ValueError(
                f"Title too long ({len(cleaned)} chars, max {SessionDB.MAX_TITLE_LENGTH})"
            )

        return cleaned

    def set_session_title(self, session_id: str, title: str) -> bool:
        """Set or update a session's title.

        Returns True if session was found and title was set.
        Raises ValueError if title is already in use by another session,
        or if the title fails validation (too long, invalid characters).
        Empty/whitespace-only strings are normalized to None (clearing the title).
        """
        title = self.sanitize_title(title)
        def _do(conn):
            if title:
                # Check uniqueness (allow the same session to keep its own title)
                cursor = conn.execute(
                    "SELECT id FROM sessions WHERE title = ? AND id != ?",
                    (title, session_id),
                )
                conflict = cursor.fetchone()
                if conflict:
                    raise ValueError(
                        f"Title '{title}' is already in use by session {conflict['id']}"
                    )
            cursor = conn.execute(
                "UPDATE sessions SET title = ? WHERE id = ?",
                (title, session_id),
            )
            return cursor.rowcount
        rowcount = self._execute_write(_do)
        return rowcount > 0

    def get_session_title(self, session_id: str) -> Optional[str]:
        """Get the title for a session, or None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return row["title"] if row else None

    def get_session_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        """Look up a session by exact title. Returns session dict or None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE title = ?", (title,)
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def resolve_session_by_title(self, title: str) -> Optional[str]:
        """Resolve a title to a session ID, preferring the latest in a lineage.

        If the exact title exists, returns that session's ID.
        If not, searches for "title #N" variants and returns the latest one.
        If the exact title exists AND numbered variants exist, returns the
        latest numbered variant (the most recent continuation).
        """
        # First try exact match
        exact = self.get_session_by_title(title)

        # Also search for numbered variants: "title #2", "title #3", etc.
        # Escape SQL LIKE wildcards (%, _) in the title to prevent false matches
        escaped = title.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id, title, started_at FROM sessions "
                "WHERE title LIKE ? ESCAPE '\\' ORDER BY started_at DESC",
                (f"{escaped} #%",),
            )
            numbered = cursor.fetchall()

        if numbered:
            # Return the most recent numbered variant
            return numbered[0]["id"]
        elif exact:
            return exact["id"]
        return None

    def get_next_title_in_lineage(self, base_title: str) -> str:
        """Generate the next title in a lineage (e.g., "my session" → "my session #2").

        Strips any existing " #N" suffix to find the base name, then finds
        the highest existing number and increments.
        """
        # Strip existing #N suffix to find the true base
        match = re.match(r'^(.*?) #(\d+)$', base_title)
        if match:
            base = match.group(1)
        else:
            base = base_title

        # Find all existing numbered variants
        # Escape SQL LIKE wildcards (%, _) in the base to prevent false matches
        escaped = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE title = ? OR title LIKE ? ESCAPE '\\'",
                (base, f"{escaped} #%"),
            )
            existing = [row["title"] for row in cursor.fetchall()]

        if not existing:
            return base  # No conflict, use the base name as-is

        # Find the highest number
        max_num = 1  # The unnumbered original counts as #1
        for t in existing:
            m = re.match(r'^.* #(\d+)$', t)
            if m:
                max_num = max(max_num, int(m.group(1)))

        return f"{base} #{max_num + 1}"

    def list_sessions_rich(
        self,
        source: str = None,
        exclude_sources: List[str] = None,
        limit: int = 20,
        offset: int = 0,
        include_children: bool = False,
    ) -> List[Dict[str, Any]]:
        """List sessions with preview (first user message) and last active timestamp.

        Returns dicts with keys: id, source, model, title, started_at, ended_at,
        message_count, preview (first 60 chars of first user message),
        last_active (timestamp of last message).

        Uses a single query with correlated subqueries instead of N+2 queries.

        By default, child sessions (subagent runs, compression continuations)
        are excluded.  Pass ``include_children=True`` to include them.
        """
        where_clauses = []
        params = []

        if not include_children:
            where_clauses.append("s.parent_session_id IS NULL")

        if source:
            where_clauses.append("s.source = ?")
            params.append(source)
        if exclude_sources:
            placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({placeholders})")
            params.extend(exclude_sources)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT s.*,
                COALESCE(
                    (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                     FROM messages m
                     WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                     ORDER BY m.timestamp, m.id LIMIT 1),
                    ''
                ) AS _preview_raw,
                COALESCE(
                    (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                    s.started_at
                ) AS last_active
            FROM sessions s
            {where_sql}
            ORDER BY s.started_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        with self._lock:
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()
        sessions = []
        for row in rows:
            s = dict(row)
            # Build the preview from the raw substring
            raw = s.pop("_preview_raw", "").strip()
            if raw:
                text = raw[:60]
                s["preview"] = text + ("..." if len(raw) > 60 else "")
            else:
                s["preview"] = ""
            sessions.append(s)

        return sessions

    # =========================================================================
    # Message storage
    # =========================================================================

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str = None,
        tool_name: str = None,
        tool_calls: Any = None,
        tool_call_id: str = None,
        token_count: int = None,
        finish_reason: str = None,
        reasoning: str = None,
        reasoning_details: Any = None,
        codex_reasoning_items: Any = None,
    ) -> int:
        """
        Append a message to a session. Returns the message row ID.

        Also increments the session's message_count (and tool_call_count
        if role is 'tool' or tool_calls is present).
        """
        # Serialize structured fields to JSON before entering the write txn
        reasoning_details_json = (
            json.dumps(reasoning_details)
            if reasoning_details else None
        )
        codex_items_json = (
            json.dumps(codex_reasoning_items)
            if codex_reasoning_items else None
        )
        tool_calls_json = json.dumps(tool_calls) if tool_calls else None

        # Pre-compute tool call count
        num_tool_calls = 0
        if tool_calls is not None:
            num_tool_calls = len(tool_calls) if isinstance(tool_calls, list) else 1

        def _do(conn):
            cursor = conn.execute(
                """INSERT INTO messages (session_id, role, content, tool_call_id,
                   tool_calls, tool_name, timestamp, token_count, finish_reason,
                   reasoning, reasoning_details, codex_reasoning_items)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    role,
                    content,
                    tool_call_id,
                    tool_calls_json,
                    tool_name,
                    time.time(),
                    token_count,
                    finish_reason,
                    reasoning,
                    reasoning_details_json,
                    codex_items_json,
                ),
            )
            msg_id = cursor.lastrowid

            # Update counters
            if num_tool_calls > 0:
                conn.execute(
                    """UPDATE sessions SET message_count = message_count + 1,
                       tool_call_count = tool_call_count + ? WHERE id = ?""",
                    (num_tool_calls, session_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                    (session_id,),
                )
            return msg_id

        return self._execute_write(_do)

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Load all messages for a session, ordered by timestamp."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp, id",
                (session_id,),
            )
            rows = cursor.fetchall()
        result = []
        for row in rows:
            msg = dict(row)
            if msg.get("tool_calls"):
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize tool_calls in get_messages, falling back to []")
                    msg["tool_calls"] = []
            result.append(msg)
        return result

    def get_messages_as_conversation(self, session_id: str) -> List[Dict[str, Any]]:
        """
        Load messages in the OpenAI conversation format (role + content dicts).
        Used by the gateway to restore conversation history.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT role, content, tool_call_id, tool_calls, tool_name, "
                "reasoning, reasoning_details, codex_reasoning_items "
                "FROM messages WHERE session_id = ? ORDER BY timestamp, id",
                (session_id,),
            )
            rows = cursor.fetchall()
        messages = []
        for row in rows:
            msg = {"role": row["role"], "content": row["content"]}
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["tool_name"]:
                msg["tool_name"] = row["tool_name"]
            if row["tool_calls"]:
                try:
                    msg["tool_calls"] = json.loads(row["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize tool_calls in conversation replay, falling back to []")
                    msg["tool_calls"] = []
            # Restore reasoning fields on assistant messages so providers
            # that replay reasoning (OpenRouter, OpenAI, Nous) receive
            # coherent multi-turn reasoning context.
            if row["role"] == "assistant":
                if row["reasoning"]:
                    msg["reasoning"] = row["reasoning"]
                if row["reasoning_details"]:
                    try:
                        msg["reasoning_details"] = json.loads(row["reasoning_details"])
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("Failed to deserialize reasoning_details, falling back to None")
                        msg["reasoning_details"] = None
                if row["codex_reasoning_items"]:
                    try:
                        msg["codex_reasoning_items"] = json.loads(row["codex_reasoning_items"])
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("Failed to deserialize codex_reasoning_items, falling back to None")
                        msg["codex_reasoning_items"] = None
            messages.append(msg)
        return messages

    # =========================================================================
    # Search
    # =========================================================================

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """Sanitize user input for safe use in FTS5 MATCH queries.

        FTS5 has its own query syntax where characters like ``"``, ``(``, ``)``,
        ``+``, ``*``, ``{``, ``}`` and bare boolean operators (``AND``, ``OR``,
        ``NOT``) have special meaning.  Passing raw user input directly to
        MATCH can cause ``sqlite3.OperationalError``.

        Strategy:
        - Preserve properly paired quoted phrases (``"exact phrase"``)
        - Strip unmatched FTS5-special characters that would cause errors
        - Wrap unquoted hyphenated and dotted terms in quotes so FTS5
          matches them as exact phrases instead of splitting on the
          hyphen/dot (e.g. ``chat-send``, ``P2.2``, ``my-app.config.ts``)
        """
        # Step 1: Extract balanced double-quoted phrases and protect them
        # from further processing via numbered placeholders.
        _quoted_parts: list = []

        def _preserve_quoted(m: re.Match) -> str:
            _quoted_parts.append(m.group(0))
            return f"\x00Q{len(_quoted_parts) - 1}\x00"

        sanitized = re.sub(r'"[^"]*"', _preserve_quoted, query)

        # Step 2: Strip remaining (unmatched) FTS5-special characters
        sanitized = re.sub(r'[+{}()\"^]', " ", sanitized)

        # Step 3: Collapse repeated * (e.g. "***") into a single one,
        # and remove leading * (prefix-only needs at least one char before *)
        sanitized = re.sub(r"\*+", "*", sanitized)
        sanitized = re.sub(r"(^|\s)\*", r"\1", sanitized)

        # Step 4: Remove dangling boolean operators at start/end that would
        # cause syntax errors (e.g. "hello AND" or "OR world")
        sanitized = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", sanitized.strip())
        sanitized = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", sanitized.strip())

        # Step 5: Wrap unquoted dotted and/or hyphenated terms in double
        # quotes.  FTS5's tokenizer splits on dots and hyphens, turning
        # ``chat-send`` into ``chat AND send`` and ``P2.2`` into ``p2 AND 2``.
        # Quoting preserves phrase semantics.  A single pass avoids the
        # double-quoting bug that would occur if dotted and hyphenated
        # patterns were applied sequentially (e.g. ``my-app.config``).
        sanitized = re.sub(r"\b(\w+(?:[.-]\w+)+)\b", r'"\1"', sanitized)

        # Step 6: Restore preserved quoted phrases
        for i, quoted in enumerate(_quoted_parts):
            sanitized = sanitized.replace(f"\x00Q{i}\x00", quoted)

        return sanitized.strip()

    def search_messages(
        self,
        query: str,
        source_filter: List[str] = None,
        exclude_sources: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Full-text search across session messages using FTS5.

        Supports FTS5 query syntax:
          - Simple keywords: "docker deployment"
          - Phrases: '"exact phrase"'
          - Boolean: "docker OR kubernetes", "python NOT java"
          - Prefix: "deploy*"

        Returns matching messages with session metadata, content snippet,
        and surrounding context (1 message before and after the match).
        """
        if not query or not query.strip():
            return []

        query = self._sanitize_fts5_query(query)
        if not query:
            return []

        # Build WHERE clauses dynamically
        where_clauses = ["messages_fts MATCH ?"]
        params: list = [query]

        if source_filter is not None:
            source_placeholders = ",".join("?" for _ in source_filter)
            where_clauses.append(f"s.source IN ({source_placeholders})")
            params.extend(source_filter)

        if exclude_sources is not None:
            exclude_placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({exclude_placeholders})")
            params.extend(exclude_sources)

        if role_filter:
            role_placeholders = ",".join("?" for _ in role_filter)
            where_clauses.append(f"m.role IN ({role_placeholders})")
            params.extend(role_filter)

        where_sql = " AND ".join(where_clauses)
        params.extend([limit, offset])

        sql = f"""
            SELECT
                m.id,
                m.session_id,
                m.role,
                snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                m.content,
                m.timestamp,
                m.tool_name,
                s.source,
                s.model,
                s.started_at AS session_started
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE {where_sql}
            ORDER BY rank
            LIMIT ? OFFSET ?
        """

        with self._lock:
            try:
                cursor = self._conn.execute(sql, params)
            except sqlite3.OperationalError:
                # FTS5 query syntax error despite sanitization — return empty
                return []
            matches = [dict(row) for row in cursor.fetchall()]

        # Add surrounding context (1 message before + after each match).
        # Done outside the lock so we don't hold it across N sequential queries.
        for match in matches:
            try:
                with self._lock:
                    ctx_cursor = self._conn.execute(
                        """SELECT role, content FROM messages
                           WHERE session_id = ? AND id >= ? - 1 AND id <= ? + 1
                           ORDER BY id""",
                        (match["session_id"], match["id"], match["id"]),
                    )
                    context_msgs = [
                        {"role": r["role"], "content": (r["content"] or "")[:200]}
                        for r in ctx_cursor.fetchall()
                    ]
                match["context"] = context_msgs
            except Exception:
                match["context"] = []

        # Remove full content from result (snippet is enough, saves tokens)
        for match in matches:
            match.pop("content", None)

        return matches

    def search_sessions(
        self,
        source: str = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List sessions, optionally filtered by source."""
        with self._lock:
            if source:
                cursor = self._conn.execute(
                    "SELECT * FROM sessions WHERE source = ? ORDER BY started_at DESC LIMIT ? OFFSET ?",
                    (source, limit, offset),
                )
            else:
                cursor = self._conn.execute(
                    "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # Utility
    # =========================================================================

    def session_count(self, source: str = None) -> int:
        """Count sessions, optionally filtered by source."""
        with self._lock:
            if source:
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE source = ?", (source,)
                )
            else:
                cursor = self._conn.execute("SELECT COUNT(*) FROM sessions")
            return cursor.fetchone()[0]

    def message_count(self, session_id: str = None) -> int:
        """Count messages, optionally for a specific session."""
        with self._lock:
            if session_id:
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
                )
            else:
                cursor = self._conn.execute("SELECT COUNT(*) FROM messages")
            return cursor.fetchone()[0]

    # =========================================================================
    # Export and cleanup
    # =========================================================================

    def export_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Export a single session with all its messages as a dict."""
        session = self.get_session(session_id)
        if not session:
            return None
        messages = self.get_messages(session_id)
        return {**session, "messages": messages}

    def export_all(self, source: str = None) -> List[Dict[str, Any]]:
        """
        Export all sessions (with messages) as a list of dicts.
        Suitable for writing to a JSONL file for backup/analysis.
        """
        sessions = self.search_sessions(source=source, limit=100000)
        results = []
        for session in sessions:
            messages = self.get_messages(session["id"])
            results.append({**session, "messages": messages})
        return results

    def clear_messages(self, session_id: str) -> None:
        """Delete all messages for a session and reset its counters."""
        def _do(conn):
            conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            conn.execute(
                "UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = ?",
                (session_id,),
            )
        self._execute_write(_do)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages.

        Child sessions are orphaned (parent_session_id set to NULL) rather
        than cascade-deleted, so they remain accessible independently.
        Returns True if the session was found and deleted.
        """
        def _do(conn):
            cursor = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ?", (session_id,)
            )
            if cursor.fetchone()[0] == 0:
                return False
            # Orphan child sessions so FK constraint is satisfied
            conn.execute(
                "UPDATE sessions SET parent_session_id = NULL "
                "WHERE parent_session_id = ?",
                (session_id,),
            )
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return True
        return self._execute_write(_do)

    def prune_sessions(self, older_than_days: int = 90, source: str = None) -> int:
        """Delete sessions older than N days. Returns count of deleted sessions.

        Only prunes ended sessions (not active ones).  Child sessions outside
        the prune window are orphaned (parent_session_id set to NULL) rather
        than cascade-deleted.
        """
        cutoff = time.time() - (older_than_days * 86400)

        def _do(conn):
            if source:
                cursor = conn.execute(
                    """SELECT id FROM sessions
                       WHERE started_at < ? AND ended_at IS NOT NULL AND source = ?""",
                    (cutoff, source),
                )
            else:
                cursor = conn.execute(
                    "SELECT id FROM sessions WHERE started_at < ? AND ended_at IS NOT NULL",
                    (cutoff,),
                )
            session_ids = set(row["id"] for row in cursor.fetchall())

            if not session_ids:
                return 0

            # Orphan any sessions whose parent is about to be deleted
            placeholders = ",".join("?" * len(session_ids))
            conn.execute(
                f"UPDATE sessions SET parent_session_id = NULL "
                f"WHERE parent_session_id IN ({placeholders})",
                list(session_ids),
            )

            for sid in session_ids:
                conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
                conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            return len(session_ids)

        return self._execute_write(_do)

    # =========================================================================
    # Orchestrator: Jobs
    # =========================================================================

    def create_orchestrator_job(
        self, *, job_id, job_type, requested_by, source_type=None,
        source_uri=None, source_scope=None, intent=None, priority=0,
        metadata_json=None,
    ):
        def _do(conn):
            conn.execute(
                """INSERT INTO orchestrator_jobs
                   (job_id, job_type, requested_by, source_type, source_uri,
                    source_scope, intent, priority, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, job_type, requested_by, source_type, source_uri,
                 source_scope, intent, priority,
                 json.dumps(metadata_json) if metadata_json else None,
                 time.time()),
            )
        self._execute_write(_do)

    def get_orchestrator_job(self, job_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM orchestrator_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            if d.get("metadata_json"):
                d["metadata_json"] = json.loads(d["metadata_json"])
            return d

    def update_orchestrator_job_status(self, job_id, status, error_message=None, route_class=None):
        def _do(conn):
            now = time.time()
            sets = ["status = ?"]
            params = [str(status)]
            if str(status) == "running":
                sets.append("started_at = COALESCE(started_at, ?)")
                params.append(now)
            if str(status) in ("completed", "failed", "cancelled"):
                sets.append("finished_at = ?")
                params.append(now)
            if error_message is not None:
                sets.append("error_message = ?")
                params.append(error_message)
            if route_class is not None:
                sets.append("route_class = ?")
                params.append(str(route_class))
            params.append(job_id)
            conn.execute(
                f"UPDATE orchestrator_jobs SET {', '.join(sets)} WHERE job_id = ?",
                params,
            )
        self._execute_write(_do)

    def list_orchestrator_jobs(self, *, requested_by=None, status=None,
                               route_class=None, limit=20, offset=0):
        clauses, params = [], []
        if requested_by:
            clauses.append("requested_by = ?"); params.append(requested_by)
        if status:
            clauses.append("status = ?"); params.append(str(status))
        if route_class:
            clauses.append("route_class = ?"); params.append(str(route_class))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM orchestrator_jobs{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
            result = []
            for row in rows:
                d = dict(row)
                if d.get("metadata_json"):
                    d["metadata_json"] = json.loads(d["metadata_json"])
                result.append(d)
            return result

    # =========================================================================
    # Orchestrator: Artifacts
    # =========================================================================

    def create_orchestrator_artifact(
        self, *, artifact_id, job_id, artifact_type, storage_backend,
        storage_path, mime_type=None, checksum=None, size_bytes=None,
        provenance_json=None, metadata_json=None,
    ):
        def _do(conn):
            conn.execute(
                """INSERT INTO orchestrator_artifacts
                   (artifact_id, job_id, artifact_type, storage_backend,
                    storage_path, mime_type, checksum, size_bytes,
                    provenance_json, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (artifact_id, job_id, str(artifact_type), str(storage_backend),
                 storage_path, mime_type, checksum, size_bytes,
                 json.dumps(provenance_json) if provenance_json else None,
                 json.dumps(metadata_json) if metadata_json else None,
                 time.time()),
            )
        self._execute_write(_do)

    def list_orchestrator_artifacts(self, job_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM orchestrator_artifacts WHERE job_id = ? ORDER BY created_at",
                (job_id,),
            ).fetchall()
            result = []
            for row in rows:
                d = dict(row)
                for col in ("provenance_json", "metadata_json"):
                    if d.get(col):
                        d[col] = json.loads(d[col])
                result.append(d)
            return result

    # =========================================================================
    # Orchestrator: Routing Decisions
    # =========================================================================

    def record_routing_decision(
        self, *, decision_id, job_id, route_class, decision_reason=None,
        confidence=0.0, manual_override=False, overridden_from=None,
    ):
        def _do(conn):
            conn.execute(
                """INSERT INTO routing_decisions
                   (decision_id, job_id, route_class, decision_reason,
                    confidence, manual_override, overridden_from, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (decision_id, job_id, str(route_class), decision_reason,
                 confidence, 1 if manual_override else 0, overridden_from,
                 time.time()),
            )
            conn.execute(
                "UPDATE orchestrator_jobs SET route_class = ? WHERE job_id = ?",
                (str(route_class), job_id),
            )
        self._execute_write(_do)

    def get_routing_decision(self, job_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM routing_decisions WHERE job_id = ? ORDER BY created_at DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["manual_override"] = bool(d.get("manual_override"))
            return d

    # =========================================================================
    # Orchestrator: Approval Requests
    # =========================================================================

    def create_approval_request(self, *, approval_id, job_id, policy_name, action_id=None):
        def _do(conn):
            conn.execute(
                """INSERT INTO approval_requests
                   (approval_id, job_id, action_id, policy_name, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (approval_id, job_id, action_id, policy_name, time.time()),
            )
        self._execute_write(_do)

    def list_approval_requests(self, job_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM approval_requests WHERE job_id = ? ORDER BY created_at",
                (job_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def resolve_approval_request(self, approval_id, *, status, resolved_by=None, resolution_note=None):
        def _do(conn):
            conn.execute(
                """UPDATE approval_requests
                   SET status = ?, resolved_by = ?, resolution_note = ?, resolved_at = ?
                   WHERE approval_id = ?""",
                (status, resolved_by, resolution_note, time.time(), approval_id),
            )
        self._execute_write(_do)

    # =========================================================================
    # Knowledge Layer: Layer Assignments
    # =========================================================================

    def create_layer_assignment(
        self, *, assignment_id, job_id, route_class, knowledge_tier,
        decision_reason=None, confidence=0.0, manual_override=False,
        overridden_from_tier=None,
    ):
        def _do(conn):
            conn.execute(
                """INSERT INTO layer_assignments
                   (assignment_id, job_id, route_class, knowledge_tier,
                    decision_reason, confidence, manual_override,
                    overridden_from_tier, decided_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (assignment_id, job_id, str(route_class), str(knowledge_tier),
                 decision_reason, confidence, 1 if manual_override else 0,
                 overridden_from_tier, time.time()),
            )
        self._execute_write(_do)

    def get_layer_assignment(self, job_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM layer_assignments WHERE job_id = ? ORDER BY decided_at DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["manual_override"] = bool(d.get("manual_override"))
            return d

    # =========================================================================
    # Knowledge Layer: Working Artifacts
    # =========================================================================

    def create_working_artifact(
        self, *, working_id, job_id, artifact_kind, destination_backend="working_repo",
        destination_path, title=None, evidence_id=None, metadata_json=None,
    ):
        def _do(conn):
            conn.execute(
                """INSERT INTO working_artifacts
                   (working_id, job_id, evidence_id, artifact_kind,
                    destination_backend, destination_path, title, metadata_json,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (working_id, job_id, evidence_id, str(artifact_kind),
                 destination_backend, destination_path, title,
                 json.dumps(metadata_json) if metadata_json else None,
                 time.time(), time.time()),
            )
        self._execute_write(_do)

    def get_working_artifact(self, working_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM working_artifacts WHERE working_id = ?", (working_id,)
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            if d.get("metadata_json"):
                d["metadata_json"] = json.loads(d["metadata_json"])
            return d

    def list_working_artifacts(self, job_id=None, *, status=None, limit=50):
        clauses, params = [], []
        if job_id:
            clauses.append("job_id = ?"); params.append(job_id)
        if status:
            clauses.append("status = ?"); params.append(str(status))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM working_artifacts{where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
            result = []
            for row in rows:
                d = dict(row)
                if d.get("metadata_json"):
                    d["metadata_json"] = json.loads(d["metadata_json"])
                result.append(d)
            return result

    def update_working_artifact_status(self, working_id, status):
        def _do(conn):
            conn.execute(
                "UPDATE working_artifacts SET status = ?, updated_at = ? WHERE working_id = ?",
                (str(status), time.time(), working_id),
            )
        self._execute_write(_do)

    # =========================================================================
    # Knowledge Layer: Canonical Candidates
    # =========================================================================

    def create_canonical_candidate(
        self, *, candidate_id, job_id, evidence_id=None, working_id=None,
        status="pending_review", metadata_json=None,
    ):
        def _do(conn):
            conn.execute(
                """INSERT INTO canonical_candidates
                   (candidate_id, job_id, evidence_id, working_id, status,
                    metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (candidate_id, job_id, evidence_id, working_id, str(status),
                 json.dumps(metadata_json) if metadata_json else None,
                 time.time()),
            )
        self._execute_write(_do)

    def get_canonical_candidate(self, candidate_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM canonical_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            if d.get("metadata_json"):
                d["metadata_json"] = json.loads(d["metadata_json"])
            return d

    def list_canonical_candidates(self, *, status=None, limit=20):
        if status:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM canonical_candidates WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (str(status), limit),
                ).fetchall()
        else:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM canonical_candidates ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("metadata_json"):
                d["metadata_json"] = json.loads(d["metadata_json"])
            result.append(d)
        return result

    def update_candidate_status(self, candidate_id, status):
        def _do(conn):
            sets = ["status = ?"]
            params = [str(status)]
            if str(status) in ("approved_for_publish", "rejected"):
                sets.append("reviewed_at = ?")
                params.append(time.time())
            params.append(candidate_id)
            conn.execute(
                f"UPDATE canonical_candidates SET {', '.join(sets)} WHERE candidate_id = ?",
                params,
            )
        self._execute_write(_do)

    # =========================================================================
    # Knowledge Layer: Canonical Publications
    # =========================================================================

    def create_canonical_publication(
        self, *, publication_id, candidate_id, destination_repo,
        entry_ids_json=None, files_changed_json=None,
        validation_status="pending", published_at=None, published_by=None,
    ):
        def _do(conn):
            conn.execute(
                """INSERT INTO canonical_publications
                   (publication_id, candidate_id, destination_repo,
                    entry_ids_json, files_changed_json, validation_status,
                    published_at, published_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (publication_id, candidate_id, destination_repo,
                 json.dumps(entry_ids_json) if entry_ids_json else None,
                 json.dumps(files_changed_json) if files_changed_json else None,
                 str(validation_status), published_at, published_by),
            )
        self._execute_write(_do)

    # =========================================================================
    # Knowledge Layer: Promotion Decisions
    # =========================================================================

    def create_promotion_decision(
        self, *, decision_id, candidate_id, status, policy_name=None,
        requested_at=None, resolved_at=None, resolved_by=None,
        resolution_note=None,
    ):
        def _do(conn):
            conn.execute(
                """INSERT INTO promotion_decisions
                   (decision_id, candidate_id, status, policy_name,
                    requested_at, resolved_at, resolved_by, resolution_note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (decision_id, candidate_id, str(status), policy_name,
                 requested_at or time.time(), resolved_at, resolved_by,
                 resolution_note),
            )
        self._execute_write(_do)

    # =========================================================================
    # Knowledge Layer: Lineage Records
    # =========================================================================

    def create_lineage_record(
        self, *, lineage_id, root_evidence_id,
        working_ids=None, candidate_ids=None, publication_ids=None,
    ):
        def _do(conn):
            conn.execute(
                """INSERT OR REPLACE INTO lineage_records
                   (lineage_id, root_evidence_id, working_ids_json,
                    candidate_ids_json, publication_ids_json, last_updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (lineage_id, root_evidence_id,
                 json.dumps(working_ids or []),
                 json.dumps(candidate_ids or []),
                 json.dumps(publication_ids or []),
                 time.time()),
            )
        self._execute_write(_do)

    def get_lineage_record(self, root_evidence_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lineage_records WHERE root_evidence_id = ? ORDER BY last_updated_at DESC LIMIT 1",
                (root_evidence_id,),
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            for col in ("working_ids_json", "candidate_ids_json", "publication_ids_json"):
                if d.get(col):
                    d[col] = json.loads(d[col])
            return d

    # =========================================================================
    # Knowledge Layer: Consumption Policies
    # =========================================================================

    def create_consumption_policy(
        self, *, policy_id, consumer_name, mode="canonical_first",
        fallback_allowed=True, working_visibility="none", metadata_json=None,
    ):
        def _do(conn):
            conn.execute(
                """INSERT OR REPLACE INTO consumption_policies
                   (policy_id, consumer_name, mode, fallback_allowed,
                    working_visibility, metadata_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (policy_id, consumer_name, str(mode),
                 1 if fallback_allowed else 0,
                 str(working_visibility),
                 json.dumps(metadata_json) if metadata_json else None,
                 time.time(), time.time()),
            )
        self._execute_write(_do)

    def get_consumption_policy(self, consumer_name):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM consumption_policies WHERE consumer_name = ?",
                (consumer_name,),
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["fallback_allowed"] = bool(d.get("fallback_allowed"))
            if d.get("metadata_json"):
                d["metadata_json"] = json.loads(d["metadata_json"])
            return d

    # =========================================================================
    # Productivity: Contact Roles (003)
    # =========================================================================

    def create_contact_role(
        self, *, contact_id, display_name, role="pending",
        language="pt-BR", approved_capabilities=None, approved_by=None,
    ):
        def _do(conn):
            now = time.time()
            approved_at = now if role == "owner" or approved_by else None
            conn.execute(
                """INSERT OR IGNORE INTO contact_roles
                   (contact_id, display_name, role, language,
                    approved_capabilities, approved_by, approved_at,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (contact_id, display_name, str(role), language,
                 json.dumps(approved_capabilities) if approved_capabilities else None,
                 approved_by, approved_at, now, now),
            )
        self._execute_write(_do)

    def get_contact_role(self, contact_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM contact_roles WHERE contact_id = ?",
                (str(contact_id),),
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            if d.get("approved_capabilities"):
                d["approved_capabilities"] = json.loads(d["approved_capabilities"])
            return d

    def update_contact_role(
        self, contact_id, *, role=None, language=None,
        approved_capabilities=None, approved_by=None,
    ):
        def _do(conn):
            sets = ["updated_at = ?"]
            params = [time.time()]
            if role is not None:
                sets.append("role = ?")
                params.append(str(role))
                if str(role) in ("owner", "contact"):
                    sets.append("approved_at = COALESCE(approved_at, ?)")
                    params.append(time.time())
            if language is not None:
                sets.append("language = ?")
                params.append(language)
            if approved_capabilities is not None:
                sets.append("approved_capabilities = ?")
                params.append(json.dumps(approved_capabilities))
            if approved_by is not None:
                sets.append("approved_by = ?")
                params.append(approved_by)
            params.append(str(contact_id))
            conn.execute(
                f"UPDATE contact_roles SET {', '.join(sets)} WHERE contact_id = ?",
                params,
            )
        self._execute_write(_do)

    def list_contact_roles(self, *, role=None):
        if role:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM contact_roles WHERE role = ? ORDER BY created_at DESC",
                    (str(role),),
                ).fetchall()
        else:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM contact_roles ORDER BY created_at DESC"
                ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("approved_capabilities"):
                d["approved_capabilities"] = json.loads(d["approved_capabilities"])
            result.append(d)
        return result

    def block_contact_role(self, contact_id):
        """Block a contact. Silent — no notification, just denies access."""
        self.update_contact_role(str(contact_id), role="blocked")

    # =========================================================================
    # Productivity: Shared Lists (003)
    # =========================================================================

    def create_shared_list(
        self, *, list_id, name, list_type="shopping", created_by,
    ):
        def _do(conn):
            now = time.time()
            conn.execute(
                """INSERT INTO shared_lists
                   (list_id, name, list_type, status, created_by,
                    created_at, updated_at)
                   VALUES (?, ?, ?, 'active', ?, ?, ?)""",
                (list_id, name, str(list_type), str(created_by), now, now),
            )
        self._execute_write(_do)

    def get_shared_list(self, list_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM shared_lists WHERE list_id = ?", (list_id,),
            ).fetchone()
            return dict(row) if row else None

    def find_shared_list_by_name(self, name):
        """Case-insensitive name match on active lists. Returns first match."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM shared_lists WHERE LOWER(name) = LOWER(?) AND status = 'active'",
                (name,),
            ).fetchone()
            return dict(row) if row else None

    def list_shared_lists(self, *, status="active"):
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM shared_lists WHERE status = ? ORDER BY created_at",
                    (str(status),),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM shared_lists ORDER BY created_at"
                ).fetchall()
            return [dict(row) for row in rows]

    def archive_shared_list(self, list_id):
        def _do(conn):
            conn.execute(
                "UPDATE shared_lists SET status = 'archived', updated_at = ? WHERE list_id = ?",
                (time.time(), list_id),
            )
        self._execute_write(_do)

    # ----- List Items -----

    def create_list_item(
        self, *, item_id, list_id, content, added_by, added_by_name=None,
    ):
        def _do(conn):
            now = time.time()
            conn.execute(
                """INSERT INTO list_items
                   (item_id, list_id, content, checked, added_by,
                    added_by_name, created_at)
                   VALUES (?, ?, ?, 0, ?, ?, ?)""",
                (item_id, list_id, content, str(added_by),
                 added_by_name, now),
            )
            # Bump parent list updated_at
            conn.execute(
                "UPDATE shared_lists SET updated_at = ? WHERE list_id = ?",
                (now, list_id),
            )
        self._execute_write(_do)

    def get_list_items(self, list_id, *, include_checked=False):
        with self._lock:
            if include_checked:
                rows = self._conn.execute(
                    "SELECT * FROM list_items WHERE list_id = ? ORDER BY created_at",
                    (list_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM list_items WHERE list_id = ? AND checked = 0 ORDER BY created_at",
                    (list_id,),
                ).fetchall()
            return [dict(row) for row in rows]

    def get_list_item(self, item_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM list_items WHERE item_id = ?", (item_id,),
            ).fetchone()
            return dict(row) if row else None

    def update_list_item_checked(self, item_id, *, checked, checked_by=None):
        def _do(conn):
            if checked:
                conn.execute(
                    """UPDATE list_items
                       SET checked = 1, checked_by = ?, checked_at = ?
                       WHERE item_id = ?""",
                    (checked_by, time.time(), item_id),
                )
            else:
                conn.execute(
                    """UPDATE list_items
                       SET checked = 0, checked_by = NULL, checked_at = NULL
                       WHERE item_id = ?""",
                    (item_id,),
                )
        self._execute_write(_do)

    def delete_list_item(self, item_id):
        def _do(conn):
            conn.execute(
                "DELETE FROM list_items WHERE item_id = ?", (item_id,),
            )
        self._execute_write(_do)

    # =========================================================================
    # Productivity: Reminders (003)
    # =========================================================================

    def create_reminder(
        self, *, reminder_id, owner_id, title, due_at,
        google_event_id=None, status="pending",
    ):
        def _do(conn):
            conn.execute(
                """INSERT INTO reminders
                   (reminder_id, owner_id, title, due_at, google_event_id,
                    status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (reminder_id, str(owner_id), title, due_at,
                 google_event_id, str(status), time.time()),
            )
        self._execute_write(_do)

    def get_reminder(self, reminder_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reminders WHERE reminder_id = ?",
                (reminder_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_reminders(self, *, owner_id=None, status=None, limit=50):
        clauses, params = [], []
        if owner_id:
            clauses.append("owner_id = ?"); params.append(str(owner_id))
        if status:
            clauses.append("status = ?"); params.append(str(status))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM reminders{where} ORDER BY due_at ASC LIMIT ?",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def update_reminder_status(self, reminder_id, status):
        def _do(conn):
            sets = ["status = ?"]
            params = [str(status)]
            if str(status) in ("completed", "cancelled"):
                sets.append("completed_at = ?")
                params.append(time.time())
            params.append(reminder_id)
            conn.execute(
                f"UPDATE reminders SET {', '.join(sets)} WHERE reminder_id = ?",
                params,
            )
        self._execute_write(_do)

    def update_reminder_google_event_id(self, reminder_id, google_event_id):
        def _do(conn):
            conn.execute(
                "UPDATE reminders SET google_event_id = ? WHERE reminder_id = ?",
                (google_event_id, reminder_id),
            )
        self._execute_write(_do)

    def get_reminders_due_soon(self, *, within_minutes=30):
        """Get pending reminders due within N minutes."""
        cutoff = time.time() + (within_minutes * 60)
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM reminders
                   WHERE status = 'pending' AND due_at <= ?
                   ORDER BY due_at ASC""",
                (cutoff,),
            ).fetchall()
            return [dict(row) for row in rows]

    # =========================================================================
    # Productivity: Meeting Reminder Dedup (003)
    # =========================================================================

    def record_meeting_reminder_sent(self, event_id):
        def _do(conn):
            conn.execute(
                "INSERT OR IGNORE INTO meeting_reminders_sent (event_id, reminded_at) VALUES (?, ?)",
                (event_id, time.time()),
            )
        self._execute_write(_do)

    def was_meeting_reminded(self, event_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM meeting_reminders_sent WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            return row is not None

    # =========================================================================
    # Intelligence Layer: Extraction Events (021)
    # =========================================================================

    def create_extraction_event(
        self, *, extraction_id, source_type, transcript_hash, sender_id,
        sender_role, execution_mode, source_format=None, source_job_id=None,
        transcript_preview=None, transcript_full_path=None,
        actions_json=None, actions_count=0, language="pt-BR",
    ):
        def _do(conn):
            conn.execute(
                """INSERT INTO extraction_events
                   (extraction_id, source_type, source_format, source_job_id,
                    transcript_hash, transcript_preview, transcript_full_path,
                    sender_id, sender_role, actions_json, actions_count,
                    execution_mode, language, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (extraction_id, str(source_type), source_format, source_job_id,
                 transcript_hash, transcript_preview, transcript_full_path,
                 str(sender_id), str(sender_role), actions_json, actions_count,
                 str(execution_mode), language, time.time()),
            )
        self._execute_write(_do)

    def get_extraction_by_hash(self, transcript_hash):
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM extraction_events
                   WHERE transcript_hash = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (transcript_hash,),
            ).fetchone()
            return dict(row) if row else None

    def list_extraction_events(self, *, sender_id=None, limit=50):
        clauses, params = [], []
        if sender_id:
            clauses.append("sender_id = ?"); params.append(str(sender_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM extraction_events{where} "
                f"ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def update_extraction_execution_mode(self, extraction_id, execution_mode):
        def _do(conn):
            conn.execute(
                "UPDATE extraction_events SET execution_mode = ? "
                "WHERE extraction_id = ?",
                (str(execution_mode), extraction_id),
            )
        self._execute_write(_do)

    # =========================================================================
    # Intelligence Layer: Pending Previews (021)
    # =========================================================================

    def create_pending_preview(
        self, *, preview_id, extraction_id, sender_id, chat_id,
        actions_json, ttl_seconds=600,
    ):
        now = time.time()
        def _do(conn):
            # Expire any prior active preview for this sender — one active per sender.
            conn.execute(
                """UPDATE pending_previews
                   SET status = 'expired', resolved_at = ?
                   WHERE sender_id = ? AND status = 'awaiting_confirmation'""",
                (now, str(sender_id)),
            )
            conn.execute(
                """INSERT INTO pending_previews
                   (preview_id, extraction_id, sender_id, chat_id,
                    actions_json, status, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, 'awaiting_confirmation', ?, ?)""",
                (preview_id, extraction_id, str(sender_id), str(chat_id),
                 actions_json, now, now + ttl_seconds),
            )
        self._execute_write(_do)

    def get_active_preview_by_sender(self, sender_id):
        """Get active preview for sender; auto-expire if past TTL."""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM pending_previews
                   WHERE sender_id = ? AND status = 'awaiting_confirmation'
                   ORDER BY created_at DESC LIMIT 1""",
                (str(sender_id),),
            ).fetchone()
            if row is None:
                return None
            preview = dict(row)
        if preview["expires_at"] < now:
            self.update_preview_status(preview["preview_id"], "expired")
            return None
        return preview

    def update_preview_status(self, preview_id, status):
        def _do(conn):
            conn.execute(
                """UPDATE pending_previews
                   SET status = ?, resolved_at = ?
                   WHERE preview_id = ?""",
                (str(status), time.time(), preview_id),
            )
        self._execute_write(_do)

    def expire_stale_previews(self):
        now = time.time()
        def _do(conn):
            conn.execute(
                """UPDATE pending_previews
                   SET status = 'expired', resolved_at = ?
                   WHERE status = 'awaiting_confirmation' AND expires_at < ?""",
                (now, now),
            )
        self._execute_write(_do)

    # =========================================================================
    # Intelligence Layer: Entity Context (021)
    # =========================================================================

    def upsert_entity(
        self, *, entity_id, name, entity_type="person",
        context_snippets_json=None,
    ):
        """Insert a new entity or bump last_mentioned_at + mention_count."""
        name_lower = name.strip().lower()
        now = time.time()
        def _do(conn):
            existing = conn.execute(
                "SELECT entity_id, mention_count FROM entity_context "
                "WHERE name_lower = ?",
                (name_lower,),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE entity_context
                       SET last_mentioned_at = ?, mention_count = ?, updated_at = ?
                       WHERE entity_id = ?""",
                    (now, existing["mention_count"] + 1, now, existing["entity_id"]),
                )
                return existing["entity_id"]
            conn.execute(
                """INSERT INTO entity_context
                   (entity_id, name, name_lower, entity_type,
                    context_snippets_json, first_mentioned_at, last_mentioned_at,
                    mention_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (entity_id, name, name_lower, str(entity_type),
                 context_snippets_json, now, now, now),
            )
            return entity_id
        return self._execute_write(_do)

    def get_entity_by_name(self, name):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM entity_context WHERE name_lower = ?",
                (name.strip().lower(),),
            ).fetchone()
            return dict(row) if row else None

    def append_entity_context_snippet(self, entity_id, snippets_json):
        def _do(conn):
            conn.execute(
                """UPDATE entity_context
                   SET context_snippets_json = ?, updated_at = ?
                   WHERE entity_id = ?""",
                (snippets_json, time.time(), entity_id),
            )
        self._execute_write(_do)

    def list_entities(self, *, entity_type=None, limit=100):
        clauses, params = [], []
        if entity_type:
            clauses.append("entity_type = ?"); params.append(str(entity_type))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM entity_context{where} "
                f"ORDER BY last_mentioned_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def update_entity_mention_count(self, entity_id, mention_count):
        def _do(conn):
            conn.execute(
                """UPDATE entity_context
                   SET mention_count = ?, updated_at = ?
                   WHERE entity_id = ?""",
                (mention_count, time.time(), entity_id),
            )
        self._execute_write(_do)

    # =========================================================================
    # Intelligence Layer: Harness Scenarios Cache (021)
    # =========================================================================

    def upsert_scenario_from_yaml(
        self, *, scenario_id, name, category, steps_json, target_bot,
        target_chat_id, yaml_path, yaml_hash, description=None,
        preconditions_json=None, estimated_duration_ms=None,
    ):
        now = time.time()
        def _do(conn):
            conn.execute(
                """INSERT INTO harness_scenarios_cache
                   (scenario_id, name, category, description, preconditions_json,
                    steps_json, estimated_duration_ms, target_bot, target_chat_id,
                    yaml_path, yaml_hash, loaded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(scenario_id) DO UPDATE SET
                       name = excluded.name,
                       category = excluded.category,
                       description = excluded.description,
                       preconditions_json = excluded.preconditions_json,
                       steps_json = excluded.steps_json,
                       estimated_duration_ms = excluded.estimated_duration_ms,
                       target_bot = excluded.target_bot,
                       target_chat_id = excluded.target_chat_id,
                       yaml_path = excluded.yaml_path,
                       yaml_hash = excluded.yaml_hash,
                       loaded_at = excluded.loaded_at""",
                (scenario_id, name, str(category), description,
                 preconditions_json, steps_json, estimated_duration_ms,
                 target_bot, str(target_chat_id), yaml_path, yaml_hash, now),
            )
        self._execute_write(_do)

    def list_scenarios(self, *, category=None):
        clauses, params = [], []
        if category:
            clauses.append("category = ?"); params.append(str(category))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM harness_scenarios_cache{where} "
                f"ORDER BY scenario_id ASC",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def get_scenario_by_id(self, scenario_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM harness_scenarios_cache WHERE scenario_id = ?",
                (scenario_id,),
            ).fetchone()
            return dict(row) if row else None

    def clear_scenarios_cache(self):
        def _do(conn):
            conn.execute("DELETE FROM harness_scenarios_cache")
        self._execute_write(_do)

    # =========================================================================
    # Intelligence Layer: Harness Runs (021)
    # =========================================================================

    def create_harness_run(
        self, *, run_id, scenario_id, triggered_by, trigger_source,
        last_passing_run_id=None,
    ):
        def _do(conn):
            conn.execute(
                """INSERT INTO harness_runs
                   (run_id, scenario_id, triggered_by, trigger_source,
                    started_at, status, last_passing_run_id)
                   VALUES (?, ?, ?, ?, ?, 'running', ?)""",
                (run_id, scenario_id, triggered_by, str(trigger_source),
                 time.time(), last_passing_run_id),
            )
        self._execute_write(_do)

    def complete_harness_run(
        self, run_id, *, status, steps_json=None,
        regression_flags_json=None, error_message=None,
    ):
        finished = time.time()
        def _do(conn):
            started = conn.execute(
                "SELECT started_at FROM harness_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            duration_ms = None
            if started is not None:
                duration_ms = int((finished - started["started_at"]) * 1000)
            conn.execute(
                """UPDATE harness_runs
                   SET finished_at = ?, duration_ms = ?, status = ?,
                       steps_json = ?, regression_flags_json = ?,
                       error_message = ?
                   WHERE run_id = ?""",
                (finished, duration_ms, str(status), steps_json,
                 regression_flags_json, error_message, run_id),
            )
        self._execute_write(_do)

    def list_harness_runs(self, *, limit=20):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM harness_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_harness_runs_by_scenario(self, scenario_id, *, limit=20):
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM harness_runs WHERE scenario_id = ?
                   ORDER BY started_at DESC LIMIT ?""",
                (scenario_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_last_passing_run(self, scenario_id):
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM harness_runs
                   WHERE scenario_id = ? AND status = 'pass'
                   ORDER BY started_at DESC LIMIT 1""",
                (scenario_id,),
            ).fetchone()
            return dict(row) if row else None

    # =========================================================================
    # Intelligence Layer: Transcript ↔ Extraction ↔ Job Lineage (021)
    # =========================================================================

    def link_extraction_to_job(
        self, *, link_id, extraction_id, job_id, working_artifact_id=None,
    ):
        def _do(conn):
            conn.execute(
                """INSERT INTO transcript_extraction_links
                   (link_id, extraction_id, job_id, working_artifact_id, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (link_id, extraction_id, job_id, working_artifact_id, time.time()),
            )
        self._execute_write(_do)

    def get_extraction_job_ids(self, extraction_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id FROM transcript_extraction_links "
                "WHERE extraction_id = ? ORDER BY created_at ASC",
                (extraction_id,),
            ).fetchall()
            return [row["job_id"] for row in rows]

    # =========================================================================
    # 022 Sentinels: credentials_store (Fernet ciphertext blobs)
    # =========================================================================

    def put_credential_ciphertext(self, *, credential_id, kind, ciphertext, label=None):
        def _do(conn):
            conn.execute(
                """INSERT INTO credentials_store
                   (credential_id, kind, ciphertext, label, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (credential_id, str(kind), ciphertext, label, time.time()),
            )
        self._execute_write(_do)

    def get_credential_ciphertext(self, credential_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM credentials_store WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()
            return dict(row) if row else None

    def rotate_credential(self, credential_id, new_ciphertext):
        def _do(conn):
            conn.execute(
                "UPDATE credentials_store SET ciphertext = ?, rotated_at = ? "
                "WHERE credential_id = ?",
                (new_ciphertext, time.time(), credential_id),
            )
        self._execute_write(_do)

    def delete_credential(self, credential_id):
        def _do(conn):
            conn.execute(
                "DELETE FROM credentials_store WHERE credential_id = ?",
                (credential_id,),
            )
        self._execute_write(_do)

    def list_credentials_by_kind(self, kind=None):
        """Metadata-only listing. NEVER returns ciphertext — only id/kind/label/timestamps."""
        clauses, params = [], []
        if kind:
            clauses.append("kind = ?"); params.append(str(kind))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT credential_id, kind, label, created_at, rotated_at "
                f"FROM credentials_store{where} ORDER BY created_at DESC",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    # =========================================================================
    # 022 Sentinels: teams_watches
    # =========================================================================

    def create_teams_watch(
        self, *, watch_id, owner_id, resource_type, ms_resource_id,
        alias=None, client_state=None, delivery_mode="realtime",
    ):
        def _do(conn):
            conn.execute(
                """INSERT INTO teams_watches
                   (watch_id, alias, resource_type, ms_resource_id,
                    client_state, delivery_mode, owner_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (watch_id, alias, str(resource_type), ms_resource_id,
                 client_state, str(delivery_mode), str(owner_id), time.time()),
            )
        self._execute_write(_do)

    def get_teams_watch(self, watch_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM teams_watches WHERE watch_id = ?", (watch_id,),
            ).fetchone()
            return dict(row) if row else None

    def find_teams_watch_by_resource(self, owner_id, ms_resource_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM teams_watches "
                "WHERE owner_id = ? AND ms_resource_id = ?",
                (str(owner_id), ms_resource_id),
            ).fetchone()
            return dict(row) if row else None

    def list_teams_watches(self, owner_id=None):
        clauses, params = [], []
        if owner_id:
            clauses.append("owner_id = ?"); params.append(str(owner_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM teams_watches{where} ORDER BY created_at DESC",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def update_teams_watch_subscription(
        self, watch_id, *, subscription_id, expires_at,
    ):
        def _do(conn):
            conn.execute(
                """UPDATE teams_watches
                   SET subscription_id = ?, subscription_expires_at = ?,
                       delivery_mode = 'realtime', updated_at = ?
                   WHERE watch_id = ?""",
                (subscription_id, expires_at, time.time(), watch_id),
            )
        self._execute_write(_do)

    def update_teams_watch_mode(self, watch_id, delivery_mode, polling_cursor=None):
        def _do(conn):
            conn.execute(
                """UPDATE teams_watches
                   SET delivery_mode = ?, polling_cursor = COALESCE(?, polling_cursor),
                       updated_at = ?
                   WHERE watch_id = ?""",
                (str(delivery_mode), polling_cursor, time.time(), watch_id),
            )
        self._execute_write(_do)

    def set_teams_watch_mute(
        self, watch_id, *, mute_until=None,
        quiet_hours_start=None, quiet_hours_end=None,
    ):
        def _do(conn):
            conn.execute(
                """UPDATE teams_watches
                   SET mute_until = ?, quiet_hours_start = ?,
                       quiet_hours_end = ?, updated_at = ?
                   WHERE watch_id = ?""",
                (mute_until, quiet_hours_start, quiet_hours_end,
                 time.time(), watch_id),
            )
        self._execute_write(_do)

    def touch_teams_watch_event(self, watch_id):
        def _do(conn):
            conn.execute(
                "UPDATE teams_watches SET last_event_at = ? WHERE watch_id = ?",
                (time.time(), watch_id),
            )
        self._execute_write(_do)

    def delete_teams_watch(self, watch_id):
        def _do(conn):
            conn.execute(
                "DELETE FROM teams_watches WHERE watch_id = ?", (watch_id,),
            )
        self._execute_write(_do)

    def find_teams_watch_by_client_state(self, client_state: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM teams_watches WHERE client_state = ?",
                (client_state,),
            ).fetchone()
            return dict(row) if row else None

    # =========================================================================
    # 022 Sentinels: mail_accounts
    # =========================================================================

    def create_mail_account(
        self, *, account_id, alias, host, username, auth_method,
        credential_ref, owner_id, port=993, connection_state="disconnected",
    ):
        def _do(conn):
            conn.execute(
                """INSERT INTO mail_accounts
                   (account_id, alias, host, port, username, auth_method,
                    credential_ref, connection_state, owner_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (account_id, alias, host, port, username, str(auth_method),
                 credential_ref, str(connection_state), str(owner_id),
                 time.time()),
            )
        self._execute_write(_do)

    def get_mail_account(self, account_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mail_accounts WHERE account_id = ?", (account_id,),
            ).fetchone()
            return dict(row) if row else None

    def find_mail_account_by_alias(self, owner_id, alias):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mail_accounts WHERE owner_id = ? AND alias = ?",
                (str(owner_id), alias),
            ).fetchone()
            return dict(row) if row else None

    def list_mail_accounts(self, owner_id=None):
        clauses, params = [], []
        if owner_id:
            clauses.append("owner_id = ?"); params.append(str(owner_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM mail_accounts{where} ORDER BY created_at DESC",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def update_mail_account_state(
        self, account_id, state, *, last_error=None,
    ):
        def _do(conn):
            now = time.time()
            if str(state) == "live":
                conn.execute(
                    """UPDATE mail_accounts
                       SET connection_state = ?, last_error = NULL,
                           last_successful_sync_at = ?, updated_at = ?
                       WHERE account_id = ?""",
                    (str(state), now, now, account_id),
                )
            else:
                conn.execute(
                    """UPDATE mail_accounts
                       SET connection_state = ?, last_error = ?, updated_at = ?
                       WHERE account_id = ?""",
                    (str(state), last_error, now, account_id),
                )
        self._execute_write(_do)

    def delete_mail_account(self, account_id):
        def _do(conn):
            conn.execute(
                "DELETE FROM mail_watches WHERE account_id = ?", (account_id,),
            )
            conn.execute(
                "DELETE FROM mail_accounts WHERE account_id = ?", (account_id,),
            )
        self._execute_write(_do)

    # =========================================================================
    # 022 Sentinels: mail_watches
    # =========================================================================

    def create_mail_watch(self, *, watch_id, account_id, folder):
        def _do(conn):
            conn.execute(
                """INSERT INTO mail_watches
                   (watch_id, account_id, folder, created_at)
                   VALUES (?, ?, ?, ?)""",
                (watch_id, account_id, folder, time.time()),
            )
        self._execute_write(_do)

    def list_mail_watches_by_account(self, account_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mail_watches WHERE account_id = ? "
                "ORDER BY created_at ASC",
                (account_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_all_mail_watches(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mail_watches ORDER BY created_at ASC",
            ).fetchall()
            return [dict(row) for row in rows]

    def update_mail_watch_cursor(
        self, watch_id, *, last_seen_uid, uidvalidity,
    ):
        def _do(conn):
            conn.execute(
                """UPDATE mail_watches
                   SET last_seen_uid = ?, uidvalidity = ?,
                       last_activity_at = ?, reconnect_attempts = 0
                   WHERE watch_id = ?""",
                (last_seen_uid, uidvalidity, time.time(), watch_id),
            )
        self._execute_write(_do)

    def update_mail_watch_state(
        self, watch_id, idle_state, *, increment_reconnect=False,
    ):
        def _do(conn):
            if increment_reconnect:
                conn.execute(
                    """UPDATE mail_watches
                       SET idle_state = ?, last_activity_at = ?,
                           reconnect_attempts = reconnect_attempts + 1
                       WHERE watch_id = ?""",
                    (str(idle_state), time.time(), watch_id),
                )
            else:
                conn.execute(
                    """UPDATE mail_watches
                       SET idle_state = ?, last_activity_at = ?
                       WHERE watch_id = ?""",
                    (str(idle_state), time.time(), watch_id),
                )
        self._execute_write(_do)

    def delete_mail_watch(self, watch_id):
        def _do(conn):
            conn.execute(
                "DELETE FROM mail_watches WHERE watch_id = ?", (watch_id,),
            )
        self._execute_write(_do)

    # =========================================================================
    # 022 Sentinels: backfill_jobs
    # =========================================================================

    def create_backfill_job(
        self, *, job_id, account_id, scope_hash, folders_json, owner_id,
        since_ts=None, until_ts=None, rate_limit_msgs_per_min=60,
    ):
        def _do(conn):
            conn.execute(
                """INSERT INTO backfill_jobs
                   (job_id, account_id, scope_hash, folders_json, since_ts,
                    until_ts, rate_limit_msgs_per_min, state, owner_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)""",
                (job_id, account_id, scope_hash, folders_json, since_ts,
                 until_ts, rate_limit_msgs_per_min, str(owner_id)),
            )
        self._execute_write(_do)

    def get_backfill_job(self, job_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM backfill_jobs WHERE job_id = ?", (job_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_active_backfill_job(self, account_id, scope_hash):
        """Return the running or queued job for this (account, scope) pair."""
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM backfill_jobs
                   WHERE account_id = ? AND scope_hash = ?
                     AND state IN ('queued', 'running', 'paused')
                   ORDER BY COALESCE(started_at, 0) DESC LIMIT 1""",
                (account_id, scope_hash),
            ).fetchone()
            return dict(row) if row else None

    def list_backfill_jobs_by_account(self, account_id, *, limit=20):
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM backfill_jobs WHERE account_id = ?
                   ORDER BY COALESCE(started_at, 0) DESC LIMIT ?""",
                (account_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def start_backfill_job(self, job_id, *, total_estimated):
        def _do(conn):
            conn.execute(
                """UPDATE backfill_jobs
                   SET state = 'running', started_at = ?, total_estimated = ?
                   WHERE job_id = ?""",
                (time.time(), total_estimated, job_id),
            )
        self._execute_write(_do)

    def update_backfill_progress(
        self, job_id, *, processed_count, failed_count,
    ):
        def _do(conn):
            conn.execute(
                """UPDATE backfill_jobs
                   SET processed_count = ?, failed_count = ?
                   WHERE job_id = ?""",
                (processed_count, failed_count, job_id),
            )
        self._execute_write(_do)

    def update_backfill_cursor(self, job_id, resumption_cursor_json):
        def _do(conn):
            conn.execute(
                "UPDATE backfill_jobs SET resumption_cursor_json = ? "
                "WHERE job_id = ?",
                (resumption_cursor_json, job_id),
            )
        self._execute_write(_do)

    def pause_backfill_job(self, job_id):
        def _do(conn):
            conn.execute(
                "UPDATE backfill_jobs SET state = 'paused' WHERE job_id = ?",
                (job_id,),
            )
        self._execute_write(_do)

    def complete_backfill_job(self, job_id):
        def _do(conn):
            conn.execute(
                """UPDATE backfill_jobs
                   SET state = 'completed', finished_at = ? WHERE job_id = ?""",
                (time.time(), job_id),
            )
        self._execute_write(_do)

    def fail_backfill_job(self, job_id, error_message):
        def _do(conn):
            conn.execute(
                """UPDATE backfill_jobs
                   SET state = 'failed', error_message = ?, finished_at = ?
                   WHERE job_id = ?""",
                (error_message, time.time(), job_id),
            )
        self._execute_write(_do)

    def list_resumable_backfill_jobs(self):
        """Jobs in running or paused state — used at gateway startup to resume."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM backfill_jobs WHERE state IN ('running', 'paused')",
            ).fetchall()
            return [dict(row) for row in rows]
