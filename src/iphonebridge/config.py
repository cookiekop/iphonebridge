"""Single source of truth for static configuration.

Everything here can be overridden later via env vars or a TOML config file;
for Phase 1 hard-coding is fine.
"""
from __future__ import annotations

import os
from pathlib import Path


def _load_local_env() -> None:
    """Source ~/.config/iphonebridge/local.env into os.environ before we
    read settings. Mirrors what systemd's `EnvironmentFile=` does for the
    daemon, so the CLI gets the same config when invoked from a fresh
    shell without anyone having to `source` anything.

    Anything already in os.environ wins — explicit env > local.env."""
    config_path = (
        Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        / "iphonebridge" / "local.env"
    )
    if not config_path.exists():
        return
    try:
        for raw in config_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k:
                os.environ.setdefault(k, v)
    except OSError:
        pass


_load_local_env()

HEADLESS: bool = os.environ.get("IPHONEBRIDGE_HEADLESS", "0").lower() in {"1", "true", "yes", "on"}


# ---- target device ------------------------------------------------------

IPHONE_MAC: str = os.environ.get("IPHONEBRIDGE_MAC", "AA:BB:CC:DD:EE:FF")
"""BD_ADDR of the paired iPhone. Set IPHONEBRIDGE_MAC env var to your
iPhone's MAC, or put it in ~/.config/iphonebridge/local.env which the
systemd user unit will source. The default is a placeholder — `doctor`
will refuse to pass until you've overridden it."""

ADAPTER: str = os.environ.get("IPHONEBRIDGE_ADAPTER", "hci0")
"""Local Bluetooth adapter."""

# ---- BlueZ identity dance (per spike/RESULTS.md §1) ---------------------

# Class-of-Device: A/V Hands-Free Device. iOS surfaces MAP/PBAP toggles
# only when the adapter presents itself with this CoD class.
COD_MAJOR: int = 4   # Audio/Video
COD_MINOR: int = 8   # = bits 7-2 → 0x02 = Hands-Free Device

ANCS_SOLICIT_UUID: str = "7905F431-B5CE-4E99-A40F-4B1E122D00D0"
"""Apple Notification Center Service UUID. Used in the BLE advert's
SolicitUUIDs field; required for the iOS toggles to surface, even though
we're not actually consuming ANCS in Phase 1."""

BLE_ADVERT_LOCAL_NAME: str = "pop-os-ibridge"

# ---- runtime paths ------------------------------------------------------

_state_home = Path(
    os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state")
) / "iphonebridge"

STATE_DIR: Path = _state_home
EVENTS_JSONL: Path = _state_home / "events.jsonl"
CONTACTS_DB: Path = _state_home / "contacts.sqlite"

def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)

# ---- dbus paths used in the daemon --------------------------------------

BLE_ADVERT_DBUS_PATH: str = "/com/gabriel/iphonebridge/ancs_advert"
