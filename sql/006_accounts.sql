-- Accounts (one admin, any number of submitters), e-mail tokens, who submitted a batch, and categories on sources.
-- Safe on a database with data: only new tables and new columns with defaults; existing rows keep working.

CREATE TABLE users (
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  email                 text NOT NULL,
  nickname              text NOT NULL DEFAULT '',
  password_hash         text NOT NULL,
  role                  text NOT NULL DEFAULT 'submitter' CHECK (role IN ('admin', 'submitter')),
  lang                  text NOT NULL DEFAULT 'ja' CHECK (lang IN ('ja', 'zh', 'en')),   -- mails and the default UI
  confirmed_at          timestamptz,             -- e-mail confirmed; an unconfirmed account cannot log in
  disabled_at           timestamptz,             -- set by the admin; a disabled account cannot log in
  must_change_password  boolean NOT NULL DEFAULT false,   -- after the admin set a password for the user
  session_version       integer NOT NULL DEFAULT 0,       -- bumped on password change / disable: old sessions end
  created_at            timestamptz NOT NULL DEFAULT now(),
  last_login_at         timestamptz
);
CREATE UNIQUE INDEX users_email ON users (lower(email));
-- exactly one admin at most: the first user who confirmed the e-mail
CREATE UNIQUE INDEX users_one_admin ON users (role) WHERE role = 'admin';

-- links in mails: only the sha256 of the token is stored; single use, with an expiry
CREATE TABLE email_tokens (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id       bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  purpose       text NOT NULL CHECK (purpose IN ('confirm', 'reset')),
  token_sha256  text NOT NULL UNIQUE,
  expires_at    timestamptz NOT NULL,
  used_at       timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX email_tokens_user ON email_tokens (user_id, purpose);

-- who submitted a batch; NULL = the admin (crawlers with the ingest token, everything from before accounts)
ALTER TABLE batches ADD COLUMN owner_id bigint REFERENCES users ON DELETE SET NULL;
CREATE INDEX batches_owner ON batches (owner_id, started_at DESC);
ALTER TABLE audit_log ADD COLUMN user_id bigint REFERENCES users ON DELETE SET NULL;

-- categories: a source is a category. Its Japanese name and whether / where the home page shows it as a tab.
-- Built-in = a source with its own parser (kind syllabus, site, bbs); kind 'upload' = generic documents, which the
-- admin may create and delete.
ALTER TABLE sources ADD COLUMN label_ja text,
                    ADD COLUMN show_on_home boolean NOT NULL DEFAULT false,
                    ADD COLUMN sort_order integer NOT NULL DEFAULT 100;

-- the home page's tabs so far (hard-coded before), so nothing changes for visitors
UPDATE sources SET label_ja = '公式サイト', show_on_home = true, sort_order = 10 WHERE name = 'shonan-it-site';
UPDATE sources SET label_ja = 'シラバス', show_on_home = true, sort_order = 20 WHERE name = 'shonan-it-syllabus';
UPDATE sources SET label_ja = '掲示板', show_on_home = true, sort_order = 30 WHERE name = 'sitoss-bbs';
