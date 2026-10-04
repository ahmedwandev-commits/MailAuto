"""Loads config/*.yaml and the .env credentials file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

try:  # python-dotenv is optional
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    pass


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"Missing config file: {path}")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass
class Credentials:
    portal_username: str
    portal_password: str = field(repr=False)
    cem_username: str = ""
    cem_password: str = field(default="", repr=False)
    operator_id: str = ""


@dataclass
class Config:
    settings: dict
    rules: dict
    ui: dict
    root: Path
    profile: str
    creds: Credentials | None = None

    # ---- convenience -------------------------------------------------
    @property
    def portal_base(self) -> str:
        return self.settings["profiles"][self.profile]["portal_base"].rstrip("/")

    @property
    def cem_base(self) -> str:
        return self.settings["profiles"][self.profile]["cem_base"].rstrip("/")

    def portal_url(self, key: str, **fmt) -> str:
        return self.portal_base + self.settings["paths"][key].format(**fmt)

    def cem_url(self, key: str, **fmt) -> str:
        return self.cem_base + self.settings["paths"][key].format(**fmt)

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    @property
    def is_real(self) -> bool:
        return self.profile == "real"


def load_config(root: Path | None = None, profile: str | None = None,
                need_credentials: bool = True) -> Config:
    root = Path(root) if root else PROJECT_ROOT
    cfg_dir = root / "config"
    settings = _read_yaml(cfg_dir / "settings.yaml")
    rules = _read_yaml(cfg_dir / "rules.yaml")
    ui = _read_yaml(cfg_dir / "ui.yaml")

    prof = profile or os.environ.get("CEM_BOT_PROFILE") or settings.get("profile", "mock")
    if prof not in settings.get("profiles", {}):
        raise ConfigError(f"Unknown profile '{prof}'. Known: {list(settings.get('profiles', {}))}")

    cfg = Config(settings=settings, rules=rules, ui=ui, root=root, profile=prof)
    if need_credentials:
        cfg.creds = load_credentials(root)
    return cfg


def load_credentials(root: Path) -> Credentials:
    env_file = root / ".env"
    if load_dotenv and env_file.exists():
        load_dotenv(env_file, override=False)
    pu = os.environ.get("PORTAL_USERNAME", "").strip()
    pp = os.environ.get("PORTAL_PASSWORD", "")
    if not pu or not pp:
        raise ConfigError(
            "PORTAL_USERNAME / PORTAL_PASSWORD are not set. Copy .env.example to .env and fill it in.")
    cu = os.environ.get("CEM_USERNAME", "").strip() or pu
    cp = os.environ.get("CEM_PASSWORD", "") or pp
    op = os.environ.get("OPERATOR_ID", "").strip() or pu
    return Credentials(pu, pp, cu, cp, op)
