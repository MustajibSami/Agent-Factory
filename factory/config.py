"""Loads config.toml and .env. One Config object is shared by every room."""
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    """Read KEY=value lines from .env into the environment (existing values win)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.strip():
            os.environ.setdefault(key.strip(), value.strip())


class Config:
    def __init__(self, data: dict, var_dir: Path):
        self.data = data
        self.root = ROOT
        self.var = var_dir
        for sub in ("videos", "outbox/posts", "outbox/emails"):
            (self.var / sub).mkdir(parents=True, exist_ok=True)

    def get(self, dotted: str, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value) -> None:
        node = self.data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    @property
    def db_path(self) -> Path:
        return self.var / "factory.db"


def load(var_dir: Path | None = None) -> Config:
    load_env(ROOT / ".env")
    with open(ROOT / "config.toml", "rb") as f:
        data = tomllib.load(f)
    var = var_dir or Path(os.environ.get("FACTORY_VAR", ROOT / "var"))
    return Config(data, Path(var))
