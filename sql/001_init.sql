-- School information API: sources, ingest batches, documents with their full history of versions.
--
-- History: a document (one URL / one course of one year / one forum thread) keeps every distinct content it was
-- seen with (versions); every crawl records which version it saw (sightings). Nothing is overwritten.
-- "Current" is computed, never stored: the newest version from a *published* batch. So a batch can be staged and
-- reviewed, published at once, and retired (a rollback: the previous version is current again) without pointers
-- that could drift. A document is "gone" when the source's latest published full crawl did not see it.

CREATE EXTENSION IF NOT EXISTS pg_search CASCADE;  -- BM25 full text (Lindera Japanese); needs pgvector

CREATE TABLE sources (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind        text NOT NULL CHECK (kind IN ('syllabus', 'site', 'bbs', 'upload')),
  name        text NOT NULL UNIQUE,          -- e.g. 'shonan-it-syllabus'
  base_url    text,
  config      jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE batches (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id    bigint NOT NULL REFERENCES sources ON DELETE CASCADE,
  label        text NOT NULL,                -- '2026後期', 'site 2026-11-09', 'upload: guide.pdf'
  kind         text NOT NULL CHECK (kind IN ('full', 'partial')),  -- full: what it did not see is gone
  status       text NOT NULL DEFAULT 'open'
               CHECK (status IN ('open', 'staging', 'published', 'retired')),
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  published_at timestamptz,
  stats        jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX batches_source_status ON batches (source_id, status, finished_at DESC);

CREATE TABLE documents (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id      bigint NOT NULL REFERENCES sources ON DELETE CASCADE,
  key            text NOT NULL,              -- normalised URL / course key / thread id
  first_seen_at  timestamptz NOT NULL DEFAULT now(),
  last_seen_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_id, key)
);

CREATE TABLE versions (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  document_id    bigint NOT NULL REFERENCES documents ON DELETE CASCADE,
  batch_id       bigint NOT NULL REFERENCES batches ON DELETE CASCADE,  -- the batch that first saw this content
  content_hash   text NOT NULL,              -- sha256 of the original bytes (or text)
  title          text,
  url            text,
  lang           text,
  info_date      date,                       -- the information's own date (news date, syllabus year)
  mime           text,
  body           text,                       -- extracted text (NULL until parsed)
  meta           jsonb NOT NULL DEFAULT '{}',
  first_seen_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX versions_document ON versions (document_id, first_seen_at DESC, id DESC);
CREATE INDEX versions_batch ON versions (batch_id);

-- every crawl that saw a version (also the one that created it)
CREATE TABLE sightings (
  version_id  bigint NOT NULL REFERENCES versions ON DELETE CASCADE,
  batch_id    bigint NOT NULL REFERENCES batches ON DELETE CASCADE,
  seen_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (version_id, batch_id)
);
CREATE INDEX sightings_batch ON sightings (batch_id);

-- original files (PDF, HTML, DOCX …) on disk under FILES_DIR, named by their sha256
CREATE TABLE files (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  version_id  bigint NOT NULL REFERENCES versions ON DELETE CASCADE,
  sha256      text NOT NULL,
  mime        text,
  size        bigint NOT NULL,
  name        text
);
CREATE INDEX files_version ON files (version_id);

-- background work (parse, chunk, embed); workers take jobs with FOR UPDATE SKIP LOCKED
CREATE TABLE jobs (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind        text NOT NULL,
  payload     jsonb NOT NULL DEFAULT '{}',
  status      text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
  attempts    int NOT NULL DEFAULT 0,
  error       text,
  run_after   timestamptz NOT NULL DEFAULT now(),
  locked_at   timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_ready ON jobs (status, run_after) WHERE status = 'queued';

-- who did what in the admin UI / ingest API
CREATE TABLE audit_log (
  id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at       timestamptz NOT NULL DEFAULT now(),
  actor    text NOT NULL,
  action   text NOT NULL,
  target   text,
  detail   jsonb NOT NULL DEFAULT '{}'
);

-- versions that count: those from published batches
CREATE VIEW published_versions AS
  SELECT v.* FROM versions v JOIN batches b ON b.id = v.batch_id WHERE b.status = 'published';

-- the current version of every document: the newest published one
CREATE VIEW current_versions AS
  SELECT DISTINCT ON (v.document_id) v.*
  FROM published_versions v
  ORDER BY v.document_id, v.first_seen_at DESC, v.id DESC;

-- gone: the source's latest published full crawl did not see any version of the document
CREATE VIEW document_status AS
  SELECT d.id AS document_id, d.source_id, d.key, cv.id AS current_version_id,
         (lf.id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM sightings s JOIN versions v ON v.id = s.version_id
            WHERE v.document_id = d.id AND s.batch_id = lf.id)) AS gone
  FROM documents d
  LEFT JOIN current_versions cv ON cv.document_id = d.id
  LEFT JOIN LATERAL (
    SELECT b.id FROM batches b
    WHERE b.source_id = d.source_id AND b.kind = 'full' AND b.status = 'published'
    ORDER BY b.finished_at DESC NULLS LAST, b.id DESC LIMIT 1) lf ON true;
