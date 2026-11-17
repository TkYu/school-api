"""Settings from the environment (.env on the server; never committed)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://school@db:5432/school"
    # the password in a file (compose secret), so it is in no environment variable and no URL
    database_password_file: Path | None = None
    # ingest API for the admin's own scripts and crawlers: Authorization: Bearer <token>. What it submits belongs to
    # the admin. People log in with an account instead (the first one confirmed is the admin).
    ingest_token: str = ""
    session_secret: str = ""
    # e-mail for sign-up confirmation, password reset and review notices
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    # "smtp" in production; "memory" keeps mails in a list (tests only, nothing is sent)
    mail_backend: Literal["smtp", "memory"] = "smtp"
    # the address people open, for links in mails (https://…); empty = the address of the request
    public_url: str = ""
    # who may sign up: comma-separated domains (@example.ac.jp) and/or addresses; empty = nobody but the first user
    signup_allowlist: str = ""
    # true: a submitter's upload is published at once; false: it waits for the admin's review
    direct_publish: bool = False
    files_dir: Path = Path("/data/files")
    # login: this many failures per client within the window lock it out for the window
    login_max_failures: int = 5
    login_window_s: int = 900
    # uploads from the web UI
    upload_max_file_mb: int = 50
    upload_max_total_mb: int = 200

    @field_validator("direct_publish", "smtp_port", "smtp_security", "mail_backend", "upload_max_file_mb", "upload_max_total_mb", mode="before")
    @classmethod
    def _empty_is_default(cls, v, info):
        # `DIRECT_PUBLISH=` (an empty line in .env, as in .env.example) means "not set", not an error
        if isinstance(v, str) and not v.strip():
            return cls.model_fields[info.field_name].default
        return v

    def allowlist(self) -> tuple[set[str], set[str]]:
        """(domains without the @, addresses), lower case."""
        items = [x.strip().lower() for x in self.signup_allowlist.split(",") if x.strip()]
        return {x[1:] for x in items if x.startswith("@")}, {x for x in items if not x.startswith("@")}


def conninfo(cfg: Settings) -> str:
    """The database URL with the password from the secret file, when there is one."""
    from psycopg.conninfo import make_conninfo
    if cfg.database_password_file and cfg.database_password_file.exists():
        return make_conninfo(cfg.database_url, password=cfg.database_password_file.read_text().strip())
    return cfg.database_url


@lru_cache
def settings() -> Settings:
    return Settings()
