"""YAML config file loader with LRU cache."""
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path("config")


@lru_cache(maxsize=None)
def load_config(filename: str) -> Any:
    """Load a YAML file from config/ (cached after first read)."""
    config_path = CONFIG_DIR / filename
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def get_dc_owners() -> dict:
    return load_config("dc_owners.yaml")


def get_keywords() -> dict:
    return load_config("keywords.yaml")


def get_regions() -> dict:
    return load_config("regions.yaml")
