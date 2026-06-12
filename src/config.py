"""Konfiguration (config.yml) und öffentlicher Bot-State (state.json).

Alles, was Form und Ablageort von Config/State betrifft, lebt hier: Pfade,
Laden/Speichern sowie kleine Helfer, die die Config-Struktur interpretieren
(Produkt-URLs, State-Keys, Anzeige-Labels, Ping-IDs).
"""
from __future__ import annotations

import json
import logging
import os
import re

from pathlib import Path

import yaml

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yml"
STATE_PATH = ROOT / "state.json"


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("state.json unreadable, starting fresh: %s", e)
        return {}


def save_state(state: dict) -> None:
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def parse_ids(raw: str) -> list[str]:
    """Komma-/Semikolon-/Space-separierte IDs in eine Liste zerlegen."""
    return [x for x in re.split(r"[,;\s]+", raw or "") if x]


def load_ping_user_ids(cfg: dict) -> list[str]:
    """Resolve Discord user IDs from env (PING_USER_IDS, comma/semicolon/space
    separated), else fall back to `notifications.ping_user_ids` in config.

    Env-first keeps personal Discord IDs out of the public repo while still
    allowing local dev to use config.yml.
    """
    env = os.environ.get("PING_USER_IDS", "").strip()
    if env:
        return parse_ids(env)
    notif = cfg.get("notifications") or {}
    return [str(u) for u in (notif.get("ping_user_ids") or [])]


def load_ping_role_ids(cfg: dict) -> list[str]:
    """Optionale Rollen-IDs aus `notifications.ping_role_ids`."""
    notif = cfg.get("notifications") or {}
    return [str(r) for r in (notif.get("ping_role_ids") or [])]


def product_urls(product: dict) -> list[str]:
    """Normalize `url` (str) and/or `urls` (list) into a flat list of URLs."""
    urls: list[str] = []
    single = product.get("url")
    if single:
        urls.append(single)
    for u in product.get("urls") or []:
        if u and u not in urls:
            urls.append(u)
    return urls


def product_state_key(product: dict, urls: list[str]) -> str:
    """Synthetic key for combined products so per-URL data doesn't collide."""
    if len(urls) > 1:
        return f"combined:{product.get('name') or urls[0]}"
    return urls[0]


def variant_labels(cfg: dict) -> dict:
    """Optionale Anzeige-Aliase pro Variante: {match_string: label}.

    Betrifft NUR die Darstellung (Dashboard, Stats, Alerts) — das Matching
    gegen die Website und der state.json-Key bleiben der rohe watch_variants-
    String. Nötig für variable Produkte (z.B. Modafinil), wo der watch_variant
    exakt das Website-Variantenlabel treffen muss; bei simple products kann man
    den watch_variant direkt umbenennen (er ist dort nur ein Label)."""
    out: dict = {}
    for p in cfg.get("products") or []:
        for match, label in (p.get("variant_labels") or {}).items():
            out[str(match)] = str(label)
    return out
