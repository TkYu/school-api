-- Search: chunks of versions, their texts (deduplicated across versions, so unchanged paragraphs keep their vector),
-- BM25 over the texts (Lindera Japanese), one vector table per embedding model; structured syllabus fields.

CREATE TABLE texts (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sha256       text NOT NULL UNIQUE,     -- of search_text
  text         text NOT NULL,            -- what the agent reads
  search_text  text NOT NULL             -- the text with its context in front (course, faculty, section …)
);
CREATE INDEX texts_bm25 ON texts USING paradedb (id, (search_text::pdb.lindera(japanese)));

CREATE TABLE chunks (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  version_id  bigint NOT NULL REFERENCES versions ON DELETE CASCADE,
  ord         int NOT NULL,
  section     text,                      -- 授業の進め方, 指標と評価割合, 授業計画 …; a heading path for pages
  text_id     bigint NOT NULL REFERENCES texts,
  UNIQUE (version_id, ord)
);
CREATE INDEX chunks_text ON chunks (text_id);

-- ruri-v3-310m (cl-nagoya), 768 dimensions, cosine; another model gets its own table
CREATE TABLE embeddings_ruri_v3 (
  text_id    bigint PRIMARY KEY REFERENCES texts ON DELETE CASCADE,
  embedding  vector(768) NOT NULL
);
CREATE INDEX embeddings_ruri_v3_hnsw ON embeddings_ruri_v3 USING hnsw (embedding vector_cosine_ops);

-- one row per syllabus version (fields as printed; nothing is corrected)
CREATE TABLE courses (
  version_id       bigint PRIMARY KEY REFERENCES versions ON DELETE CASCADE,
  year             int,                -- 令和8年度 -> 2026
  code             text NOT NULL,      -- シラバスコード S-172R09-01
  numbering        text,
  name             text,
  name_en          text,
  faculty          text,               -- 学部: 工学部 / 情報学部 …
  department       text,               -- 学科・科目区分
  field            text,               -- 科目分野
  form             text,               -- 授業形態: 講義 / 演習 / 実験 …
  grade            text,               -- 配当年次
  term             text,               -- 学期
  required         text,               -- 必選区分
  credits          int,
  instructors      text[] NOT NULL DEFAULT '{}',
  active_learning  text,
  teacher_training text,               -- 教職科目対応
  practical        text,               -- 実践的教育対応
  eval_sum         int,                -- sum of the 全体の評価割合 row as printed (should be 100)
  notes            jsonb NOT NULL DEFAULT '{}'   -- parse remarks, e.g. {"eval_sum_not_100": true}
);
CREATE INDEX courses_code ON courses (code);
CREATE INDEX courses_filters ON courses (faculty, term, year);

CREATE TABLE course_evaluations (
  version_id  bigint NOT NULL REFERENCES versions ON DELETE CASCADE,
  item        text NOT NULL,           -- 受講態度, 授業中の活動, 予習・復習, 成果物・発表, 試験, 学修の振り返り
  percent     int NOT NULL,            -- the 全体の評価割合 row
  PRIMARY KEY (version_id, item)
);

CREATE TABLE course_sessions (
  version_id  bigint NOT NULL REFERENCES versions ON DELETE CASCADE,
  no          int NOT NULL,
  topic       text,
  prep        text,
  review      text,
  hours       numeric,
  PRIMARY KEY (version_id, no)
);
