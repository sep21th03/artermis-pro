-- Copyright 2026 Google LLC
--
-- Licensed under the Apache License, Version 2.0 (the "License");
-- you may not use this file except in compliance with the License.
-- You may obtain a copy of the License at
--
--     http://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing, software
-- distributed under the License is distributed on an "AS IS" BASIS,
-- WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
-- See the License for the specific language governing permissions and
-- limitations under the License.

-- Knowledge base for Workflow RAG. Idempotent: safe to run on every startup.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS kb_schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per tested application. `id` is a slug ("hunonic"); the Android
-- package name is kept separately because a package can be renamed while the
-- knowledge stays valid.
CREATE TABLE IF NOT EXISTS kb_app (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    package_name  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Version-scoped knowledge. A workflow with version_id IS NULL applies to every
-- version of its app; one with a version_id applies only to that version. This
-- is what stops v2 instructions from being served to a v3 build.
CREATE TABLE IF NOT EXISTS kb_app_version (
    id             TEXT PRIMARY KEY,
    app_id         TEXT NOT NULL REFERENCES kb_app(id) ON DELETE CASCADE,
    version_label  TEXT NOT NULL,
    notes          TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (app_id, version_label)
);

CREATE TABLE IF NOT EXISTS kb_workflow (
    id           TEXT PRIMARY KEY,
    app_id       TEXT NOT NULL REFERENCES kb_app(id) ON DELETE CASCADE,
    version_id   TEXT REFERENCES kb_app_version(id) ON DELETE SET NULL,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft', 'verified', 'deprecated')),
    tags         TEXT[] NOT NULL DEFAULT '{}',
    metadata     JSONB NOT NULL DEFAULT '{}',
    -- Diacritic-stripped, lower-cased projection of id+name+description+aliases
    -- +tags+step targets. Normalising in Python (not in SQL) keeps "dang xuat"
    -- and "đăng xuất" searchable as the same term.
    search_text  TEXT NOT NULL DEFAULT '',
    revision_no  INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS kb_workflow_app_idx ON kb_workflow (app_id);
CREATE INDEX IF NOT EXISTS kb_workflow_status_idx ON kb_workflow (status);
-- Bộ lọc truy hồi đọc metadata (`type`, `screen`): không có index thì mỗi lần agent hỏi KB theo
-- màn hình là một lần quét toàn bảng.
CREATE INDEX IF NOT EXISTS kb_workflow_metadata_idx
    ON kb_workflow USING GIN (metadata jsonb_path_ops);
CREATE INDEX IF NOT EXISTS kb_workflow_search_idx
    ON kb_workflow USING GIN (to_tsvector('simple', search_text));
-- Trigram index makes the alias/exact fallback cheap on large catalogues.
CREATE INDEX IF NOT EXISTS kb_workflow_search_trgm_idx
    ON kb_workflow USING GIN (search_text gin_trgm_ops);

CREATE TABLE IF NOT EXISTS kb_workflow_alias (
    workflow_id  TEXT NOT NULL REFERENCES kb_workflow(id) ON DELETE CASCADE,
    alias        TEXT NOT NULL,
    normalized   TEXT NOT NULL,
    -- Author order, so the UI shows aliases the way the tester typed them.
    ordinal      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, normalized)
);
-- Idempotent upgrade for installs created before `ordinal` existed.
ALTER TABLE kb_workflow_alias ADD COLUMN IF NOT EXISTS ordinal INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS kb_workflow_alias_norm_idx
    ON kb_workflow_alias (normalized);

CREATE TABLE IF NOT EXISTS kb_workflow_step (
    workflow_id     TEXT NOT NULL REFERENCES kb_workflow(id) ON DELETE CASCADE,
    ordinal         INTEGER NOT NULL,
    action          TEXT NOT NULL,
    target          TEXT NOT NULL DEFAULT '',
    expected_state  TEXT NOT NULL DEFAULT '',
    direction       TEXT,
    until           TEXT NOT NULL DEFAULT '',
    value           TEXT NOT NULL DEFAULT '',
    timeout_s       DOUBLE PRECISION,
    optional        BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (workflow_id, ordinal)
);

CREATE TABLE IF NOT EXISTS kb_workflow_precondition (
    workflow_id  TEXT NOT NULL REFERENCES kb_workflow(id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,
    text         TEXT NOT NULL,
    PRIMARY KEY (workflow_id, ordinal)
);

CREATE TABLE IF NOT EXISTS kb_workflow_success_criterion (
    workflow_id  TEXT NOT NULL REFERENCES kb_workflow(id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,
    text         TEXT NOT NULL,
    PRIMARY KEY (workflow_id, ordinal)
);

-- One row per (workflow, model).  Keeping every model side by side is what makes
-- a model switch a re-index instead of a data loss, and `content_hash` is what
-- keeps an unchanged workflow from being embedded again on every save.
--
-- The column is deliberately untyped `vector` rather than `vector(N)`: several
-- models with different dimensions coexist here, and pgvector cannot index a
-- column whose dimension varies.  Retrieval therefore scans exactly, which for a
-- catalogue of thousands of workflows is still milliseconds; if a deployment
-- outgrows that, pin the dimension, add an HNSW index and keep the same code.
CREATE TABLE IF NOT EXISTS kb_workflow_embedding (
    workflow_id   TEXT NOT NULL REFERENCES kb_workflow(id) ON DELETE CASCADE,
    model         TEXT NOT NULL,
    dim           INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    embedding     vector NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workflow_id, model)
);

-- Append-only edit history, so "who changed the logout steps and when" is
-- answerable without keeping a second copy of the live table.
CREATE TABLE IF NOT EXISTS kb_workflow_revision (
    id           BIGSERIAL PRIMARY KEY,
    workflow_id  TEXT NOT NULL REFERENCES kb_workflow(id) ON DELETE CASCADE,
    revision_no  INTEGER NOT NULL,
    snapshot     JSONB NOT NULL,
    change_note  TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS kb_workflow_revision_wf_idx
    ON kb_workflow_revision (workflow_id, revision_no DESC);

-- What the agent was actually told, per run.  `context_sent` is stored already
-- redacted by the caller: this table must never hold credentials.
CREATE TABLE IF NOT EXISTS kb_retrieval_log (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              TEXT,
    query               TEXT NOT NULL,
    app_id              TEXT,
    version_id          TEXT,
    retrieved           JSONB NOT NULL DEFAULT '[]',
    chosen_workflow_id  TEXT,
    context_sent        TEXT NOT NULL DEFAULT '',
    timings_ms          JSONB NOT NULL DEFAULT '{}',
    degraded            BOOLEAN NOT NULL DEFAULT FALSE,
    -- How the run actually ended, written back after the task finished so the
    -- UI can answer "did the knowledge we injected help?" instead of only
    -- "was something retrieved?".
    outcome             JSONB,
    outcome_recorded_at TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Idempotent upgrade for installs created before outcome tracking existed.
ALTER TABLE kb_retrieval_log ADD COLUMN IF NOT EXISTS outcome JSONB;
ALTER TABLE kb_retrieval_log ADD COLUMN IF NOT EXISTS outcome_recorded_at TIMESTAMPTZ;
-- Bộ lọc metadata của lần truy hồi (rỗng với truy hồi theo câu hỏi như cũ). Additive: bản cũ
-- chạy được ngay, và store còn một đường INSERT dự phòng cho KB chưa migrate.
ALTER TABLE kb_retrieval_log ADD COLUMN IF NOT EXISTS filters JSONB NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS kb_retrieval_log_run_idx ON kb_retrieval_log (run_id);
CREATE INDEX IF NOT EXISTS kb_retrieval_log_time_idx ON kb_retrieval_log (created_at DESC);

INSERT INTO kb_schema_version (version) VALUES (1) ON CONFLICT DO NOTHING;

