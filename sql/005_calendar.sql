-- The school calendar (学年暦), parsed from its PDF (app/parsers/calendar.py): one row per event, tied to the PDF's
-- version, so the current calendar is the current version's rows and every earlier one stays with its history.
CREATE TABLE calendar_events (
  version_id  bigint NOT NULL REFERENCES versions ON DELETE CASCADE,
  ord         int NOT NULL,
  year        int NOT NULL,              -- 年度 (令和8(2026)年度 → 2026; January–March belong to the year before)
  program     text NOT NULL,             -- 学部 / 大学院
  term        text NOT NULL,             -- 前学期 / 後学期
  label       text NOT NULL,             -- 行事 as printed
  start_date  date NOT NULL,
  end_date    date NOT NULL,
  date_text   text NOT NULL,             -- 期日又は期間 as printed
  note        text,                      -- a parse remark for the admin review (e.g. weekday did not fit)
  PRIMARY KEY (version_id, ord)
);
CREATE INDEX calendar_events_dates ON calendar_events (year, program, start_date);
