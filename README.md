# school-api — a search API over a school's public material

Read-only search over a school's syllabi, official website (pages and PDFs), student forum and uploaded documents,
built for AI agents (e.g. a skill that calls it with `curl`) and for people (a plain Japanese search page). Staff
upload documents through a small admin web UI with accounts; crawlers feed it through an ingest API. Every document
keeps its full history.

- Hybrid search: BM25 (pg_search, Japanese tokenizer) and vectors (pgvector, ruri-v3), merged; filters for
  syllabus fields (faculty, term, year, form …) and the academic calendar.
- History: nothing is overwritten. A batch is staged, reviewed, published, and can be taken down again (search falls
  back to the previous versions).
- Accounts: the first confirmed account is the admin; staff from allowed domains sign up by e-mail and upload PDFs,
  Office files, text files or zips into categories; the admin reviews them (or `DIRECT_PUBLISH`).
- Admin UI in Japanese, Chinese and English; server-rendered, no front-end build, no third-party scripts.
- One host with Docker: the API is published on `127.0.0.1` only, for a reverse proxy with TLS; the database is on an
  internal network; data lives in named volumes. CPU by default, NVIDIA GPU with `compose.gpu.yml`.

Details: [`docs/accounts.md`](docs/accounts.md) (accounts, categories, uploads).

## Layout

```
app/          FastAPI app (main.py) · auth.py (default deny) · accounts.py, users.py, mail.py (accounts)
              uploads.py, files.py (web uploads, zip) · admin.py (admin pages) · ingest.py (ingest API)
              search.py, courses.py, calendar_api.py, documents.py (public API) · home.py (search page)
              processing.py, parsers/ (PDF, syllabus, calendar, forum, Office, text) · worker.py · cli.py
              i18n.py, messages.py (ja / zh / en) · templates/
sql/          migrations, applied in order by `school-api migrate`
tests/        pytest against a real PostgreSQL 18 + pg_search + pgvector
crawlers/     official-site and forum crawlers (run outside the server)
Dockerfile    one image for the API and the worker
compose.yml       db (ParadeDB, PostgreSQL 18) · api (127.0.0.1:8000) · worker (mail, models); CPU only
compose.gpu.yml   the same with a CUDA build of PyTorch and the GPU for api and worker
deploy/       nginx.conf (snippet) · setup-host.sh · deploy.sh · backup.sh
```

## Public API (no auth; search is rate-limited per client)

| | |
|---|---|
| `GET /v1/health` | `{"ok": true}` |
| `GET\|POST /v1/search` | search (`q`, `source` = category ID, syllabus filters …); POST takes the same as a JSON body (for terminals that mangle Japanese arguments) |
| `GET\|POST /v1/courses` · `GET /v1/courses/{code}` · `GET /v1/courses/facets` | syllabus courses by fields; one course; the values of each field |
| `GET\|POST /v1/calendar` | the academic calendar (events, terms, registration periods) for a date |
| `GET /v1/documents/{id}[?as_of=…]` · `/versions` · `/versions/{vid}` | a document's current version (or the one current at a time), its published versions |
| `GET /` | the search page (Japanese); tabs = the categories the admin shows on it |

## Ingest API (the admin's scripts: `Authorization: Bearer $INGEST_TOKEN`, or the admin's session)

| | |
|---|---|
| `PUT /v1/admin/sources/{name}` | create / update a category (`kind`: syllabus, site, bbs, upload) |
| `POST /v1/admin/batches` | open a batch (`source`, `label`, `kind`: full / partial) |
| `PUT /v1/admin/batches/{id}/documents` | put one document (`key`, `url`, `title`, `info_date`, `text` and / or `raw_base64` …): unchanged content = a sighting, changed = a new version |
| `POST /v1/admin/batches/{id}/finish` | close it; `publish: true` publishes at once, else it waits in staging |
| `POST /v1/admin/batches/{id}/publish` · `/retire` | publish a staged batch · take it down (rollback; data stays) |
| `POST /v1/admin/documents/{id}/purge` | delete a document with its whole history (`reason` is logged) |
| `GET /v1/admin/embeddings/pending` · `PUT /v1/admin/embeddings` | vectors computed on another machine (`school-api embed-remote`) |

What the ingest token submits belongs to the admin. The CLI wraps it inside the api container:
`school-api ingest-files | ingest-site | ingest-bbs | embed-remote` (see `app/cli.py`).

## Admin UI (`/admin`)

Sign up at `/admin/signup`, log in at `/admin/login`.

- **Admin:** overview, review queue (publish / reject submissions), documents (search, hide, process again, purge,
  versions with diffs), upload (simple and advanced), categories (names, home-page tabs and order, create / delete
  custom ones), users (disable, set a new password, each user's submissions), search test, jobs, activity log.
- **Submitter:** upload (drag and drop, zip), own submissions (state, files, what was left out; withdraw / take down),
  settings (display name, language), password.

## Configuration (`.env`, see `.env.example`)

| Variable | |
|---|---|
| `INGEST_TOKEN` | bearer token for the admin's scripts and crawlers |
| `SESSION_SECRET` | signs the session cookie |
| `PUBLIC_URL` | the address people open, for links in mails |
| `SIGNUP_ALLOWLIST` | who may sign up: `@domain` and / or addresses, comma-separated; empty = only the first account |
| `DIRECT_PUBLISH` | `true`: submitters' uploads are published at once; empty / `false`: the admin reviews them |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY` (`starttls` / `ssl` / `none`), `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | mail for sign-up, password reset and review notices (sent by the worker) |
| `UPLOAD_MAX_FILE_MB`, `UPLOAD_MAX_TOTAL_MB` | web upload limits (50 / 200); behind Cloudflare's free plan a request may be at most 100 MB |
| `FILES_DIR` | where original files are kept (by sha256) |
| `API_PORT` | the port on 127.0.0.1 the API is published on (8000) |
| `COMPOSE_FILE` | `compose.gpu.yml` for the GPU version, or your own `compose.local.yml` (unset: `compose.yml`) |

## Deployment (one host with Docker)

Quick start (Docker with Compose; for the GPU version also the NVIDIA driver and Container Toolkit):

```sh
git clone https://github.com/TkYu/school-api && cd school-api
mkdir -p secrets && openssl rand -hex 24 > secrets/db_password
cp .env.example .env          # fill in INGEST_TOKEN and SESSION_SECRET (long random strings), PUBLIC_URL,
                              # SIGNUP_ALLOWLIST, SMTP_*; for the GPU: COMPOSE_FILE=compose.gpu.yml
docker compose up -d db
docker compose run --rm api school-api migrate
docker compose up -d
```

- The first start downloads the embedding model (the worker does, into the `models` volume); the API
  restarts until it is there (a few minutes), then `curl http://127.0.0.1:8000/v1/health` answers `{"ok": true}`.
- Put a reverse proxy with TLS in front (`deploy/nginx.conf` for nginx; behind Cloudflare: proxied DNS, bot checks
  off for this host so `curl` works, the origin reachable from Cloudflare only).
- **Sign up the admin right away** at `https://<your host>/admin/signup` and open the confirmation mail. Until
  then, the first person who confirms an e-mail becomes the admin; if that was not you, remove the database volume
  (`docker compose down` and `docker volume rm school-api_db`) and start again.
- Data: the named volumes `school-api_db` (PostgreSQL), `school-api_files` (original files) and `school-api_models`.
  Back up with `deploy/backup.sh` (database dump, files, settings; e.g. nightly from cron). Never copy the database
  volume while it runs.
- Your own variant (a Unix socket for the proxy, folders on the host instead of volumes, other limits): copy
  `compose.yml` to `compose.local.yml` (ignored by git), change it and set `COMPOSE_FILE=compose.local.yml` in `.env`.
  A socket needs `GUNICORN_CMD_ARGS: "--bind unix:/run/api/api.sock"` for the api service and a folder mounted on
  `/run/api`.

Updates with `deploy/deploy.sh <ssh host> [commit]` (committed state only; the host keeps the code in
`/srv/school-api`): it pushes the commit to `/srv/school-api.git` on the host and checks it out; it rebuilds and
restarts only when code, dependencies or compose files changed, and migrates only when `sql/` changed. An older
commit = rollback; the deployed commit is in `/srv/school-api/.deployed`. `deploy/setup-host.sh` prepares such a host
once (folders, database password, a `.env` with generated secrets). Upgrading a deployment from before accounts:
see [`docs/accounts.md`](docs/accounts.md#upgrading-a-deployment-from-before-accounts).

## Development

Tests need PostgreSQL 18 with pg_search and pgvector (`TEST_PG_ADMIN_URL`, a user that may create databases). They
never send mail (`MAIL_BACKEND=memory`) and use a fake embedder.

```sh
uv sync
TEST_PG_ADMIN_URL=postgresql://school:…@localhost/postgres uv run pytest
```

## Licence

MIT (`LICENSE`). The school's own documents (syllabi, the calendar PDF, pages of its website) are not part of this
repository: crawlers and uploads bring them into your instance.
