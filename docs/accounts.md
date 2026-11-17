# Accounts, categories and uploads

How people get into the admin UI, what each of them may do, and how files from the web become searchable.

## Roles

| | Admin | Submitter |
|---|---|---|
| Who | the **first account whose e-mail is confirmed**; there is only one | every account confirmed after that |
| Pages | everything under `/admin` | `/admin/my` (upload, own submissions), `/admin/settings`, `/admin/password` |
| Uploads | any category; publishes at once or leaves it for review, per upload; also the advanced upload and the ingest API (`INGEST_TOKEN`) | generic-document categories only; published at once with `DIRECT_PUBLISH=true`, else after the admin's review |
| Own submissions | — | sees and takes down (or withdraws) only their own; cannot delete |
| Users | lists everyone, disables / enables, sets a new password | — |

Default deny stays (`app/auth.py`): a route is public only when listed in `PUBLIC`; a submitter only reaches the
routes in `SUBMITTER`; everything else is the admin's. The admin account cannot be disabled; a unique index allows at
most one admin.

**Right after deploying, sign up the admin.** Until someone has confirmed an e-mail, the first person to do so becomes
the admin. If someone else got there first, wipe the database and deploy again.

## Sign-up, login, password

- Sign-up: e-mail (must match `SIGNUP_ALLOWLIST`: `@domain` entries and / or full addresses; an empty list lets only
  the very first account in), display name, password (10–200 characters, argon2). A confirmation link is mailed;
  the account works once it is opened (48 hours). The page answers the same whether or not the address already has an
  account; only the allowlist (no secret) is named.
- Login: e-mail and password; the existing per-client lockout (5 failures in 15 minutes). Unconfirmed and disabled
  accounts cannot log in.
- Forgot password: a reset link (1 hour) to a confirmed, enabled account; the page says the same for any address.
- Own password change needs the current password. A password change (reset, own change, set by the admin) or
  disabling an account ends every other session at once (`users.session_version`, checked on every request).
- The admin can set a new random password for a submitter: shown once on the page, no mail; the user must change it
  before anything else.
- Every link token is random (32 bytes), stored only as sha256, single use, and made by the worker when it sends the
  mail. Sign-up and reset requests are rate-limited per client.

## Mail

Pages queue a `send_mail` job; the worker sends it (the API container has no internet access in `compose.yml`, the
worker has). Mails are plain text in the recipient's language: confirmation, password reset, and one notice to the
admin per submission waiting for review. `MAIL_BACKEND=memory` keeps mails in a list for the tests.

## Languages

The admin UI is in Japanese, Chinese and English (`app/messages.py`; switcher in the header, then the account's
language, then the browser's). The public search page stays Japanese.

## Categories

A category is a source (`sources`): its Japanese name (`label_ja`), whether the home page shows it as a tab
(`show_on_home`) and where (`sort_order`). 「すべて」 is always the first tab. Hiding a category only affects the home
page; the API searches every category.

- **Built-in** categories have their own parser (kind `syllabus`, `site`, `bbs`): cannot be deleted.
- **Custom** categories (kind `upload`) use the generic document path; the admin creates them (the ID becomes the
  API's `source` filter and cannot change) and may delete one only after every document in it was purged.

## Uploads from the web

One upload is one batch owned by the uploader, labelled with the display name and the time. Each file becomes a
document keyed `u<user id>/<path>` in the chosen category: the same person uploading the same file name again adds a
new version of it; two people never overwrite each other. The file name (without extension) is the title.

Formats: PDF (text layer), `.docx` `.xlsx` `.pptx` (standard-library parser: headings, sheets, slides), and text-like
files (`.txt .md .csv .tsv .html .xml .json .rtf` …, encoding detected: UTF-8, UTF-16, CP932, EUC-JP). A `.zip` is
unpacked: each entry is handled the same way; nested archives, images, old Office formats and other files are skipped
and listed (in the result message and on the submission's page). Limits: `UPLOAD_MAX_FILE_MB` per file (default 50)
and `UPLOAD_MAX_TOTAL_MB` per upload (default 200, also the uncompressed total of a zip), at most 1000 zip entries,
no entry compressed more than 200:1 above 1 MB, no encrypted entries. Entry names never touch the disk (files are
stored by hash), so `../` only becomes part of a key.

## Tables (sql/006_accounts.sql)

- `users`: e-mail (unique, case-insensitive), display name, password hash, role, language, confirmed / disabled,
  must change password, session version, last login.
- `email_tokens`: user, purpose (`confirm` / `reset`), sha256 of the token, expiry, used.
- `batches.owner_id` (NULL = the admin: crawler batches and everything from before accounts; the first admin takes
  those over when confirming), `audit_log.user_id`.
- `sources.label_ja`, `show_on_home`, `sort_order`; the migration gives the three existing home-page tabs their
  names and keeps them shown.

## Upgrading a deployment from before accounts

1. Back up (`deploy/backup.sh`).
2. In `.env`: remove `ADMIN_USER` and `ADMIN_PASSWORD_HASH`; keep `INGEST_TOKEN` and `SESSION_SECRET`; add
   `PUBLIC_URL`, `SIGNUP_ALLOWLIST`, `SMTP_*` (and `DIRECT_PUBLISH`, `UPLOAD_MAX_*_MB` if wanted).
3. Deploy: the migration runs (only new tables and columns; nothing is rewritten except the three tab names).
4. Sign up the admin at once and confirm the mail. Crawlers and scripts keep using `INGEST_TOKEN`.
5. Old sessions of the single admin login end (they carry no user id).
