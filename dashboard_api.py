# dashboard_api.py
from __future__ import annotations


# Default per-device configuration. Kept minimal; device-specific overrides live in dashboard_devices.config_json.
DEFAULT_DEVICE_CFG: Dict[str, Dict[str, Any]] = {
    "braiins": {
        # Braiins OS LAN devices: gRPC Public API primary, BOSminer PAPI fallback
        "grpc_port": 50051,
        "grpc_username": "root",
        "grpc_password": "",
        "papi_port": 4028,
    }
}


from fastapi import APIRouter, HTTPException, UploadFile, File, Body, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from pydantic import ConfigDict, AliasChoices
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone
import time
import os
import json
import ipaddress
import hashlib
import mimetypes
import sqlite3
import re
import socket
import shutil
import subprocess
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import db

logger = logging.getLogger("dashboard_api")

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# Resolve all on-disk paths relative to this file (not the process CWD). This
# avoids accidental "multiple DB/files" situations in Docker/Portainer.
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ASSET_ROOT = os.path.join(_BASE_DIR, "data", "dashboard_assets")
BG_DIR = os.path.join(ASSET_ROOT, "backgrounds")
SND_DIR = os.path.join(ASSET_ROOT, "sounds")

BUILTIN_ASSET_ROOT = os.path.join(_BASE_DIR, "builtin_assets")
# map kind -> (builtin_subdir, data_dir)
BUILTIN_KIND_DIRS = {
    "background": ("backgrounds", BG_DIR),
    "sound": ("sounds", SND_DIR),
}

DEFAULT_SETTINGS: Dict[str, Any] = {
    "refresh_interval_ms": 5000,
    "request_timeout_s": 1.2,
    "block_odds_timescale": "day",  # hour|day|month|year
    "theme": "dark",  # "dark" or "light" (front-end also keeps bb_theme localStorage)
    "clean_mode": False,
    "card_transparency_pct": 8,
    "hashrate_unit": "GH",
    "hashrate_decimals": 2,
    "rejected_share_red_threshold_pct": 1.0,
    "max_columns": 0,  # 0 = auto
    "compact_cards": True,
    "enable_scan": True,
    "scan_default_cidr": "192.168.0.1/24",
    "braiins": {
        "grpc_port": 50051,
        "grpc_username": "root",
        "grpc_password": "",
        "papi_port": 4028,
    },
    "animations": {
        "enabled": True,
        "coin_drop": True,
        "shake_on_share": True,
        "sound_on_share": False,
        "sound_volume": 0.35,
        "max_coins": 35,
    },
    "thresholds": {
        "chip_temp": {
            "warn": 60.0,
            "danger": 70.0,
            "warn_color": "#f59e0b",
            "danger_color": "#ef4444",
        },
        "vrm_temp": {
            "warn": 70.0,
            "danger": 85.0,
            "warn_color": "#f59e0b",
            "danger_color": "#ef4444",
        },
        "hashrate": {
            "warn_pct_of_10m": 70.0,
            "warn_color": "#f59e0b",
        },
        "offline": {
            "grace_s": 15,
        },
    },
    "assets": {
        "active_background_id": None,
        "active_sound_id": None,
    },
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dirs() -> None:
    os.makedirs(BG_DIR, exist_ok=True)
    os.makedirs(SND_DIR, exist_ok=True)


def _ensure_tables() -> None:
    _ensure_dirs()
    conn = db._get_conn()
    cur = conn.cursor()

    # Canonical schema for new installs.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            ip TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            poll_type TEXT NOT NULL DEFAULT 'http',
            config_json TEXT,
            last_seen TEXT,
            last_poll TEXT,
            online INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            last_info_json TEXT
        );
        """
    )

    def _table_info():
        cur.execute("PRAGMA table_info(dashboard_devices);")
        rows = cur.fetchall()
        cols = {r[1] for r in rows}  # (cid, name, type, notnull, dflt_value, pk)
        types = {r[1]: (r[2] or "") for r in rows}
        return cols, types

    def _add_col(col_name: str, ddl: str) -> None:
        cols, _types = _table_info()
        if col_name in cols:
            return
        cur.execute(ddl)

    # Protocol-aware polling.
    _add_col("poll_type", "ALTER TABLE dashboard_devices ADD COLUMN poll_type TEXT NOT NULL DEFAULT 'http';")

    # Optional per-device config JSON (credentials, ports, etc.)
    _add_col("config_json", "ALTER TABLE dashboard_devices ADD COLUMN config_json TEXT;")

    # Dashboard health/status fields. These are used by the polling loop; if they
    # are missing, the dashboard can 500 with "no such column".
    _add_col("last_seen", "ALTER TABLE dashboard_devices ADD COLUMN last_seen TEXT;")
    _add_col("last_poll", "ALTER TABLE dashboard_devices ADD COLUMN last_poll TEXT;")
    _add_col("online", "ALTER TABLE dashboard_devices ADD COLUMN online INTEGER NOT NULL DEFAULT 0;")
    _add_col("last_error", "ALTER TABLE dashboard_devices ADD COLUMN last_error TEXT;")
    _add_col("last_info_json", "ALTER TABLE dashboard_devices ADD COLUMN last_info_json TEXT;")

    # Cleanup migration for the historical missing-comma bug.
    # Old schema could yield a column type like: "TEXT\n            last_seen TEXT".
    cols, types = _table_info()
    cfg_type = str(types.get("config_json") or "")
    if cfg_type and ("\n" in cfg_type or "last_seen" in cfg_type.lower()):
        tmp = "dashboard_devices__rebuild"
        cur.execute(f"DROP TABLE IF EXISTS {tmp};")
        cur.execute(
            f"""
            CREATE TABLE {tmp} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                ip TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                poll_type TEXT NOT NULL DEFAULT 'http',
                config_json TEXT,
                last_seen TEXT,
                last_poll TEXT,
                online INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                last_info_json TEXT
            );
            """
        )

        expected = [
            "id",
            "name",
            "ip",
            "created_at",
            "sort_order",
            "poll_type",
            "config_json",
            "last_seen",
            "last_poll",
            "online",
            "last_error",
            "last_info_json",
        ]

        cols, _types2 = _table_info()
        params = []
        select_parts = []
        now = _utcnow_iso()
        for c in expected:
            if c in cols:
                select_parts.append(c)
            else:
                if c == "online":
                    select_parts.append("0")
                elif c == "created_at":
                    select_parts.append("?")
                    params.append(now)
                else:
                    select_parts.append("NULL")

        cur.execute(
            f"INSERT INTO {tmp} ({', '.join(expected)}) "
            f"SELECT {', '.join(select_parts)} FROM dashboard_devices;",
            params,
        )

        cur.execute("DROP TABLE dashboard_devices;")
        cur.execute(f"ALTER TABLE {tmp} RENAME TO dashboard_devices;")

        # Keep AUTOINCREMENT sequence sane.
        try:
            cur.execute("SELECT COALESCE(MAX(id), 0) AS mx FROM dashboard_devices;")
            row = cur.fetchone()
            try:
                mx = int(row["mx"])  # sqlite3.Row
            except Exception:
                mx = int(row[0]) if row else 0
            cur.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES('dashboard_devices', ?) "
                "ON CONFLICT(name) DO UPDATE SET seq=excluded.seq;",
                (mx,),
            )
        except Exception:
            pass

    # Normalize any empty values for older installs.
    cur.execute("UPDATE dashboard_devices SET poll_type='http' WHERE poll_type IS NULL OR TRIM(poll_type)='';")
    cur.execute("UPDATE dashboard_devices SET online=0 WHERE online IS NULL;")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            settings_json TEXT NOT NULL
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,              -- 'background' or 'sound'
            filename TEXT NOT NULL,
            orig_name TEXT,
            mime TEXT,
            size_bytes INTEGER,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0
        );
        """
    )

    conn.commit()
    conn.close()


def _seed_builtin_assets() -> None:
    """
    Copy any files from builtin_assets/{sounds,backgrounds} into the corresponding
    data/dashboard_assets/{sounds,backgrounds} folder, and register them in dashboard_assets.
    Safe to run multiple times.
    """
    _ensure_dirs()

    conn = db._get_conn()
    cur = conn.cursor()

    for kind, (subdir, out_dir) in BUILTIN_KIND_DIRS.items():
        src_dir = os.path.join(BUILTIN_ASSET_ROOT, subdir)
        if not os.path.isdir(src_dir):
            continue

        seeded_ids: list[int] = []

        for name in sorted(os.listdir(src_dir)):
            if name.startswith("."):
                continue
            src_path = os.path.join(src_dir, name)
            if not os.path.isfile(src_path):
                continue

            # Keep a stable filename in data/ (nice for humans + deterministic seeding)
            safe_name = os.path.basename(name).replace(" ", "_")
            dst_path = os.path.join(out_dir, safe_name)

            # Copy only if missing (don't clobber user-modified versions)
            if not os.path.exists(dst_path):
                os.makedirs(out_dir, exist_ok=True)
                shutil.copyfile(src_path, dst_path)

            # Register in DB if missing
            mime = mimetypes.guess_type(dst_path)[0] or ("audio/wav" if kind == "sound" else "application/octet-stream")
            size = os.path.getsize(dst_path)

            # avoid duplicates by (kind, filename) or (kind, orig_name)
            cur.execute(
                """
                SELECT id FROM dashboard_assets
                WHERE kind=? AND (filename=? OR orig_name=?)
                LIMIT 1;
                """,
                (kind, safe_name, name),
            )
            row = cur.fetchone()
            if row:
                seeded_ids.append(int(row["id"]))
                continue

            cur.execute(
                """
                INSERT INTO dashboard_assets (kind, filename, orig_name, mime, size_bytes, created_at, active)
                VALUES (?, ?, ?, ?, ?, ?, 0);
                """,
                (kind, safe_name, name, mime, size, _utcnow_iso()),
            )
            seeded_ids.append(int(cur.lastrowid))

        # If nothing is active for this kind yet, activate the first seeded one
        if seeded_ids:
            cur.execute("SELECT id FROM dashboard_assets WHERE kind=? AND active=1 LIMIT 1;", (kind,))
            has_active = cur.fetchone() is not None
            if not has_active:
                first_id = seeded_ids[0]
                cur.execute("UPDATE dashboard_assets SET active=0 WHERE kind=?;", (kind,))
                cur.execute("UPDATE dashboard_assets SET active=1 WHERE id=?;", (first_id,))

    conn.commit()
    conn.close()


# Ensure tables exist at import time (keeps app.py changes minimal).
_ensure_tables()
_seed_builtin_assets()


def _deep_merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Merge b into a (copy), recursively for dicts."""
    out = json.loads(json.dumps(a))
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _get_settings() -> Dict[str, Any]:
    conn = db._get_conn()
    cur = conn.cursor()
    cur.execute("SELECT settings_json FROM dashboard_settings WHERE id = 1;")
    row = cur.fetchone()
    conn.close()
    if not row:
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        stored = json.loads(row["settings_json"])
    except Exception:
        stored = {}
    return _deep_merge(DEFAULT_SETTINGS, stored)


def _save_settings(settings: Dict[str, Any]) -> None:
    conn = db._get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO dashboard_settings (id, settings_json)
        VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET settings_json=excluded.settings_json;
        """,
        (json.dumps(settings),),
    )
    conn.commit()
    conn.close()


class DeviceCreate(BaseModel):
    """Payload for creating a device from the dashboard UI or API.

    The dashboard UI historically used camelCase field names; we accept both snake_case and
    camelCase to avoid silently dropping credentials (which caused devices to fall back to
    auto/BOSminer probes and appear offline).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    ip: str = Field(..., description="IPv4/IPv6 address")
    name: Optional[str] = None

    # Polling / protocol selection
    poll_type: Optional[str] = Field(
        default="auto",
        validation_alias=AliasChoices("poll_type", "pollType"),
        description="Polling protocol. Supported: auto, http, avalon_cgminer, bosminer_papi, braiins_grpc",
    )

    # Braiins OS gRPC auth (BOS+ / BOS) added by Mk1DzL 12/25
    grpc_username: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("grpc_username", "grpcUsername", "username", "user"),
        description="Braiins OS gRPC username (e.g., root)",
    )
    grpc_password: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("grpc_password", "grpcPassword", "password", "pass"),
        description="Braiins OS gRPC password",
    )

    # Optional ports (advanced)
    grpc_port: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("grpc_port", "grpcPort"),
        ge=1,
        le=65535,
        description="gRPC port (default 50051)",
    )
    papi_port: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("papi_port", "papiPort"),
        ge=1,
        le=65535,
        description="BOSminer PAPI port (default 4028)",
    )


class SettingsUpdate(BaseModel):
    settings: Dict[str, Any]


class ReorderPayload(BaseModel):
    device_ids: List[int]


def _validate_ip(ip: str) -> str:
    try:
        return str(ipaddress.ip_address(ip.strip()))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid IP: {ip}") from e


def _parse_device_cfg(device_row: Dict[str, Any]) -> Dict[str, Any]:
    """Parse per-device config JSON from DB row safely."""
    raw = device_row.get("config_json")
    if not raw:
        return {}
    try:
        if isinstance(raw, (dict, list)):
            return raw if isinstance(raw, dict) else {"raw": raw}
        return json.loads(raw) if isinstance(raw, str) else {}
    except Exception:
        return {}


def _list_devices() -> List[Dict[str, Any]]:
    conn = db._get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM dashboard_devices
        ORDER BY sort_order ASC, id ASC;
        """
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _get_latest_benchmark_for_ip(ip: str) -> Optional[Dict[str, Any]]:
    conn = db._get_conn()
    cur = conn.cursor()
    # Prefer most recently finished completed run; fall back to most recent run.
    cur.execute(
        """
        SELECT id, status, started_at, finished_at
        FROM benchmark_runs
        WHERE bitaxe_ip = ?
        ORDER BY COALESCE(finished_at, started_at) DESC
        LIMIT 1;
        """,
        (ip,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def _write_device_poll(
    device_id: int,
    online: bool,
    info: Optional[Dict[str, Any]],
    error: Optional[str],
    poll_type: Optional[str] = None,
) -> None:
    conn = db._get_conn()
    cur = conn.cursor()
    now = _utcnow_iso()
    last_seen = now if online else None
    cur.execute(
        """
        UPDATE dashboard_devices
        SET online = ?,
            last_poll = ?,
            last_seen = COALESCE(?, last_seen),
            last_error = ?,
            last_info_json = COALESCE(?, last_info_json),
            poll_type = COALESCE(?, poll_type)
        WHERE id = ?;
        """,
        (
            1 if online else 0,
            now,
            last_seen,
            error,
            json.dumps(info) if info is not None else None,
            poll_type,
            device_id,
        ),
    )
    conn.commit()
    conn.close()


def _cgminer_query(ip: str, cmd: str, timeout_s: float) -> str:
    # Avalon Q runs a cgminer-compatible TCP API on port 4028.
    # It expects the raw command string (no newline).
    with socket.create_connection((ip, 4028), timeout=timeout_s) as s:
        s.settimeout(timeout_s)
        s.sendall(cmd.encode("utf-8", errors="ignore"))
        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            # cgminer responses end in a pipe delimiter
            if b"|" in chunk:
                break
            if len(buf) > 250_000:
                break
        return buf.decode("utf-8", errors="replace")


def _parse_cgminer_sections(resp: str) -> List[Dict[str, str]]:
    # Split "STATUS=...|SUMMARY,...|" into a list of dicts per section
    out: List[Dict[str, str]] = []
    for sec in (resp or "").split("|"):
        sec = sec.strip()
        if not sec:
            continue
        d: Dict[str, str] = {}
        for part in sec.split(","):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            d[k.strip()] = v.strip()
        if d:
            out.append(d)
    return out


def _pick_first(sections: List[Dict[str, str]], key: str) -> Optional[Dict[str, str]]:
    for d in sections:
        if key in d:
            return d
    return None


def _extract_bracket_fields(raw: str, keys: List[str]) -> Dict[str, str]:
    """Extract fields formatted like Key[Value] from Avalon 'estats' blobs."""
    out: Dict[str, str] = {}
    if not raw:
        return out
    for k in keys:
        m = re.search(rf"\b{k}\[([^\]]+)\]", raw)
        if m:
            out[k] = m.group(1).strip()
    return out


def _sane_temp(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    # Some devices report "missing" temps as absurd values (e.g. -273).
    if x <= -200:
        return None
    return x


def _probe_avalon_q(ip: str, timeout_s: float) -> Tuple[bool, Optional[Dict[str, str]], Optional[str]]:
    try:
        v_secs = _parse_cgminer_sections(_cgminer_query(ip, "version", timeout_s))
        ver = _pick_first(v_secs, "PROD") or _pick_first(v_secs, "MODEL") or {}
        if not ver:
            return False, None, "No cgminer version response"
        return True, ver, None
    except Exception as e:
        print(f"[avalon_cgminer] probe failed for {ip}: {e}")
        return False, None, str(e)


def _poll_avalon_q(ip: str, timeout_s: float) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    try:
        v_raw = _cgminer_query(ip, "version", timeout_s)
        s_raw = _cgminer_query(ip, "summary", timeout_s)
        e_raw = _cgminer_query(ip, "estats", timeout_s)

        v_secs = _parse_cgminer_sections(v_raw)
        s_secs = _parse_cgminer_sections(s_raw)
        e_secs = _parse_cgminer_sections(e_raw)

        ver = _pick_first(v_secs, "PROD") or {}
        summ = _pick_first(s_secs, "Elapsed") or {}
        stats = _pick_first(e_secs, "STATS") or {}

        # Nano 3S-style telemetry: fields like OTemp[75], TAvg[80], FanR[21%]
        br = _extract_bracket_fields(
            e_raw,
            [
                "ITemp",
                "OTemp",
                "HBITemp",
                "HBOTemp",
                "TAvg",
                "TMax",
                "MTavg",
                "MTmax",
                "FanR",
                "Fan1",
                "Fan2",
                "Fan3",
                "Fan4",
                "Ver",
                "Power",
                "Pwr",
                "PWR",
                "POW",
                "Watts",
                "Watt",
                "Pout",
                "POUT",
                "VIN",
                "VIn",
                "Vin",
                "IIN",
                "IIn",
                "Iin",
                "PS",
            ],
        )
        for k, v in br.items():
            stats.setdefault(k, v)

        # hashrate: cgminer returns MHS (mega-hash/s). Convert to GH/s for dashboard parity.
        def mhs_to_gh(v: Any) -> Optional[float]:
            try:
                x = float(v)
                return x / 1000.0
            except Exception:
                return None

        hr_now = mhs_to_gh(summ.get("MHS 5s"))
        hr_1m = mhs_to_gh(summ.get("MHS 1m"))
        hr_5m = mhs_to_gh(summ.get("MHS 5m"))
        hr_15m = mhs_to_gh(summ.get("MHS 15m"))
        hr_avg = mhs_to_gh(summ.get("MHS av"))

        # temps: Avalon estats exposes several: ITemp, HBITemp, HBOTemp, TAvg, TMax.
        def num(v: Any) -> Optional[float]:
            try:
                return float(v)
            except Exception:
                return None

        chip_temp = _sane_temp(num(stats.get("TAvg") or stats.get("MTavg") or stats.get("HBOTemp") or stats.get("HBITemp")))
        out_temp = _sane_temp(num(stats.get("OTemp") or stats.get("HBOTemp")))
        board_temp = _sane_temp(num(stats.get("HBITemp")))
        in_temp = _sane_temp(num(stats.get("ITemp")))

        # power (W)
        def _num_from_any(v: Any) -> Optional[float]:
            if v is None:
                return None
            if isinstance(v, (int, float)):
                return float(v)
            s = str(v).strip()
            if not s:
                return None
            s = s.replace("W", "").replace("w", "").replace("V", "").replace("v", "").replace("A", "").replace("a", "")
            s = s.replace("mV", "").replace("mA", "")
            mm = re.search(r"[-+]?[0-9]*\.?[0-9]+", s)
            if not mm:
                return None
            try:
                return float(mm.group(0))
            except Exception:
                return None

        power_w: Optional[float] = None

        if power_w is None:
            ps = stats.get("PS")
            if ps is not None:
                nums = re.findall(r"[-+]?[0-9]*\.?[0-9]+", str(ps))
                if nums:
                    try:
                        w_guess = float(nums[-1])
                        ps_max = 5000 if str(ver.get("MODEL") or "").strip().upper() == "Q" or "avalon q" in str(ver.get("PROD") or "").lower() else 500
                        if 10 <= w_guess <= ps_max:
                            power_w = w_guess
                    except Exception:
                        pass

        for k in ("Power", "Pwr", "PWR", "POW", "Watts", "Watt", "Pout", "POUT"):
            pv = _num_from_any(stats.get(k))
            if pv is not None and pv > 0:
                power_w = pv
                break

        if power_w is None:
            vin = None
            iin = None
            for vk in ("VIN", "VIn", "Vin"):
                vin = _num_from_any(stats.get(vk))
                if vin is not None:
                    break
            for ik in ("IIN", "IIn", "Iin"):
                iin = _num_from_any(stats.get(ik))
                if iin is not None:
                    break

            if vin is not None and iin is not None and vin > 0 and iin > 0:
                v_volts = vin / 1000.0 if vin > 200 else vin
                i_amps = iin / 1000.0 if iin > 20 else iin
                power_w = v_volts * i_amps

        # fan
        fan_pct = None
        fr = stats.get("FanR")
        if isinstance(fr, str) and fr.endswith("%"):
            fan_pct = num(fr[:-1])
        if fan_pct is None:
            fan_pct = num(fr)

        fan_rpms = [num(stats.get(k)) for k in ("Fan1", "Fan2", "Fan3", "Fan4")]
        fan_rpms_f = [x for x in fan_rpms if x is not None]
        fan_rpm_avg = (sum(fan_rpms_f) / len(fan_rpms_f)) if fan_rpms_f else None

        # shares / best diff
        acc = num(summ.get("Accepted"))
        rej = num(summ.get("Rejected"))
        best = num(summ.get("Best Share"))

        # identity / firmware
        prod = (ver.get("PROD") or "Avalon").strip()
        model = (ver.get("MODEL") or "").strip()
        if model and (model.lower() == prod.lower() or model.lower() in prod.lower()):
            device_model = prod
        else:
            device_model = (f"{prod} {model}").strip() if model else prod

        if (not device_model or device_model.lower() == "avalon") and stats.get("Ver"):
            vv = str(stats.get("Ver"))
            base = vv.split("-", 1)[0].strip()
            if base:
                if base.lower().startswith("nano3s"):
                    device_model = "Avalon Nano 3S"
                else:
                    device_model = f"Avalon {base}"
        if device_model and device_model.lower() == "avalon nano3s":
            device_model = "Avalon Nano 3S"

        lver = ver.get("LVERSION") or ver.get("CGVERSION") or ""
        mac = ver.get("MAC") or None

        up = None
        try:
            up = int(float(summ.get("Elapsed"))) if summ.get("Elapsed") is not None else None
        except Exception:
            up = None

        info: Dict[str, Any] = {
            "deviceModel": device_model or "Avalon",
            "hostname": f"{device_model}" if device_model else "Avalon",
            "version": str(lver) if lver else None,
            "macAddr": mac,
            "uptimeSeconds": up,
            "hashRate": hr_now,
            "hashRate_1m": hr_1m,
            "hashRate_10m": hr_5m,  # closest cgminer provides
            "hashRate_1h": hr_avg,  # best long-ish signal available
            "power": power_w,
            "temp": chip_temp,
            "outTemp": out_temp,
            "boardTemp": board_temp,
            "inTemp": in_temp,
            "fanspeed": fan_pct,
            "fanrpm": fan_rpm_avg,
            "sharesAccepted": int(acc) if acc is not None else None,
            "sharesRejected": int(rej) if rej is not None else None,
            "bestDiff": best,
            "foundBlocks": int(float(summ.get("Found Blocks"))) if summ.get("Found Blocks") else None,
            "_avalon": {
                "version": ver,
                "summary": summ,
                "estats": stats,
            },
        }

        return True, info, None
    except Exception as e:
        return False, None, str(e)


# ---- Braiins OS / BOSminer support ----

_BRAIINS_TOKEN_CACHE: Dict[str, Dict[str, Any]] = {}  # ip -> {"token": str, "expires_at": float}

# Small service/method discovery cache so we can safely adapt across BOS/BOS+ builds
# (some endpoints may be missing or renamed).
_BRAIINS_GRPC_DISCOVERY_CACHE: Dict[str, Dict[str, Any]] = {}  # ip -> {"expires_at": float, "lines": set[str]}
_BRAIINS_GRPC_DISCOVERY_TTL_S = 10 * 60  # 10 minutes


def _braiins_grpc_discover(ip: str, port: int, timeout_s: float) -> set[str]:
    """Return grpcurl 'list' output lines (services and methods), cached."""
    now = time.time()
    cached = _BRAIINS_GRPC_DISCOVERY_CACHE.get(ip)
    if cached and isinstance(cached.get("lines"), set) and float(cached.get("expires_at") or 0) > now:
        return cached["lines"]

    lines: set[str] = set()
    try:
        cp = subprocess.run(
            ["grpcurl", "-plaintext", f"{ip}:{port}", "list"],
            capture_output=True,
            text=True,
            timeout=max(float(timeout_s or 1.2), 2.0),
        )
        if cp.returncode == 0 and (cp.stdout or "").strip():
            for ln in (cp.stdout or "").splitlines():
                s = ln.strip()
                if s:
                    lines.add(s)
    except Exception:
        lines = set()

    _BRAIINS_GRPC_DISCOVERY_CACHE[ip] = {
        "lines": lines,
        "expires_at": now + float(_BRAIINS_GRPC_DISCOVERY_TTL_S),
    }
    return lines


def _braiins_grpc_has_method(discovery_lines: set[str], fq_method: str) -> bool:
    # grpcurl list may return:
    # - services: braiins.bos.v1.CoolingService
    # - methods:  braiins.bos.v1.CoolingService.GetCoolingState
    # We accept either an exact method match or the service existing (then we try anyway).
    if fq_method in discovery_lines:
        return True
    svc = fq_method.split("/", 1)[0] if "/" in fq_method else fq_method.rsplit(".", 1)[0]
    if svc in discovery_lines:
        return True
    return False


def _probe_braiins_grpc(ip: str, timeout_s: float, cfg: Optional[Dict[str, Any]] = None) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """Probe Braiins OS gRPC API via grpcurl reflection (no auth required)."""
    port = int((cfg or {}).get("grpc_port") or 50051)
    try:
        cp = subprocess.run(
            ["grpcurl", "-plaintext", f"{ip}:{port}", "list"],
            capture_output=True,
            text=True,
            timeout=max(float(timeout_s or 1.2), 2.0),
        )
        if cp.returncode != 0:
            return False, None, (cp.stderr or cp.stdout or "grpcurl failed").strip()
        out = cp.stdout or ""
        if "braiins.bos.v1.AuthenticationService" in out and "braiins.bos.v1.MinerService" in out:
            return True, {"grpc_port": port}, None
        return False, None, "gRPC reflection missing expected services"
    except FileNotFoundError:
        return False, None, "grpcurl not installed"
    except Exception as e:
        return False, None, str(e)


def _braiins_grpc_login(ip: str, timeout_s: float, cfg: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[str]]:
    """Login and return token (token passed as 'authorization: <token>')."""
    port = int(cfg.get("grpc_port") or 50051)
    username = str(cfg.get("grpc_username") or cfg.get("username") or (cfg.get("auth") or {}).get("username") or "root")
    password = str(cfg.get("grpc_password") or cfg.get("password") or cfg.get("pass") or (cfg.get("auth") or {}).get("password") or "")
    if not password:
        return False, None, "Missing gRPC password"
    payload = json.dumps({"username": username, "password": password})
    try:
        cp = subprocess.run(
            ["grpcurl", "-plaintext", "-d", payload, f"{ip}:{port}", "braiins.bos.v1.AuthenticationService/Login"],
            capture_output=True,
            text=True,
            timeout=max(float(timeout_s or 1.2), 3.0),
        )
        if cp.returncode != 0:
            return False, None, (cp.stderr or cp.stdout or "grpcurl login failed").strip()
        data = json.loads(cp.stdout or "{}")
        token = data.get("token")
        if not token:
            return False, None, "No token in LoginResponse"
        return True, str(token), None
    except Exception as e:
        return False, None, str(e)


def _first_number(d: Any, keys: List[str]) -> Optional[float]:
    """Return first numeric value among candidate keys (case-insensitive), supporting nested dicts."""
    if not isinstance(d, dict):
        return None
    lower_map = {str(k).lower(): k for k in d.keys()}
    for k in keys:
        kk = str(k).lower()
        if kk in lower_map:
            v = d[lower_map[kk]]
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                try:
                    return float(v.strip())
                except Exception:
                    pass
            if isinstance(v, dict):
                for subk in ("c", "value_c", "temperature_c", "temp_c", "value", "temperature", "temp", "degreeC", "degree_c"):
                    subv = v.get(subk)
                    if isinstance(subv, (int, float)):
                        return float(subv)
                    if isinstance(subv, str):
                        try:
                            return float(subv.strip())
                        except Exception:
                            pass
    return None


def _deep_find_numbers(obj: Any, key_hints: List[str], max_hits: int = 1) -> List[float]:
    """Recursively collect numeric values where key name includes any hint."""
    hits: List[float] = []
    hints = [h.lower() for h in key_hints]

    def walk(x: Any):
        nonlocal hits
        if len(hits) >= max_hits:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                k_l = str(k).lower()
                if any(h in k_l for h in hints) and isinstance(v, (int, float, str, dict)):
                    num = _first_number({k: v}, [k])
                    if num is not None:
                        hits.append(float(num))
                        if len(hits) >= max_hits:
                            return
                walk(v)
                if len(hits) >= max_hits:
                    return
        elif isinstance(x, list):
            for it in x:
                walk(it)
                if len(hits) >= max_hits:
                    return

    walk(obj)
    return hits


def _poll_braiins_grpc_extras(ip: str, port: int, token: str, timeout_s: float) -> Dict[str, Any]:
    """Fetch extra metrics via Braiins gRPC (best-effort).

    Adds:
      - temps + fans (CoolingService/GetCoolingState)
      - uptime + hostname + version + model/identity (MinerService/GetMinerDetails)
      - pool/stratum info + per-pool stats (PoolService/GetPoolGroups)
      - foundBlocks (best-effort; often unavailable via gRPC)
    """
    out: Dict[str, Any] = {}

    discovery = _braiins_grpc_discover(ip, port, timeout_s)

    def _grpc_call_json(method: str, payload_json: str) -> Optional[Dict[str, Any]]:
        cmd = [
            "grpcurl",
            "-plaintext",
            "-d",
            payload_json,
        ]
        if token:
            cmd += ["-H", f"authorization: {token}"]
        cmd += [f"{ip}:{port}", method]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=max(0.5, float(timeout_s)),
            )
        except Exception as e:
            logger.debug("[braiins_grpc] extras grpcurl failed: %s", e)
            return None

        if proc.returncode != 0:
            # Retry Bearer once.
            if token:
                cmd2 = [
                    "grpcurl",
                    "-plaintext",
                    "-H",
                    f"authorization: Bearer {token}",
                    "-d",
                    payload_json,
                    f"{ip}:{port}",
                    method,
                ]
                try:
                    proc2 = subprocess.run(
                        cmd2,
                        capture_output=True,
                        text=True,
                        timeout=max(0.5, float(timeout_s)),
                    )
                    if proc2.returncode != 0:
                        logger.debug("[braiins_grpc] extras grpcurl nonzero: %s", (proc2.stderr or proc2.stdout or "").strip())
                        return None
                    proc = proc2
                except Exception as e:
                    logger.debug("[braiins_grpc] extras grpcurl retry failed: %s", e)
                    return None
            else:
                logger.debug("[braiins_grpc] extras grpcurl nonzero: %s", (proc.stderr or proc.stdout or "").strip())
                return None

        raw = (proc.stdout or "").strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            try:
                first_line = raw.splitlines()[0]
                return json.loads(first_line)
            except Exception:
                logger.debug("[braiins_grpc] extras JSON parse failed (first 200): %r", raw[:200])
                return None

    # --- Cooling (temps/fans)
    if _braiins_grpc_has_method(discovery, "braiins.bos.v1.CoolingService/GetCoolingState"):
        cooling = _grpc_call_json("braiins.bos.v1.CoolingService/GetCoolingState", "{}")
    else:
        cooling = None

    if isinstance(cooling, dict):
        chip_temp = None
        board_temp = None

        ht = cooling.get("highestTemperature")
        if isinstance(ht, dict):
            t = ht.get("temperature")
            if isinstance(t, dict):
                try:
                    if "degreeC" in t:
                        chip_temp = float(t.get("degreeC"))
                except Exception:
                    pass

        temps = cooling.get("temperatures")
        if isinstance(temps, list):
            for item in temps:
                if not isinstance(item, dict):
                    continue
                loc = str(item.get("location") or "")
                t = item.get("temperature")
                deg = None
                if isinstance(t, dict) and "degreeC" in t:
                    try:
                        deg = float(t.get("degreeC"))
                    except Exception:
                        deg = None
                if deg is None and "degreeC" in item:
                    try:
                        deg = float(item.get("degreeC"))
                    except Exception:
                        deg = None
                if deg is None:
                    continue
                if "CHIP" in loc and (chip_temp is None or deg > chip_temp):
                    chip_temp = deg
                if "BOARD" in loc and (board_temp is None or deg > board_temp):
                    board_temp = deg

        if chip_temp is not None:
            out["temp"] = chip_temp
        if board_temp is not None:
            out["boardTemp"] = board_temp

        fans = cooling.get("fans")
        if isinstance(fans, list) and fans:
            rpms: List[float] = []
            ratios: List[float] = []
            for f in fans:
                if not isinstance(f, dict):
                    continue
                if "rpm" in f:
                    try:
                        rpms.append(float(f.get("rpm")))
                    except Exception:
                        pass
                if "targetSpeedRatio" in f:
                    try:
                        ratios.append(float(f.get("targetSpeedRatio")))
                    except Exception:
                        pass

            if rpms:
                out["fanrpm"] = sum(rpms) / len(rpms)
            if ratios:
                out["fanspeed"] = max(ratios) * 100.0

    # --- Miner details (hostname/version/uptime)
    details = None
    if _braiins_grpc_has_method(discovery, "braiins.bos.v1.MinerService/GetMinerDetails"):
        details = _grpc_call_json("braiins.bos.v1.MinerService/GetMinerDetails", "{}")

    if isinstance(details, dict):
        hn = details.get("hostname")
        if isinstance(hn, str) and hn.strip():
            out["hostname"] = hn.strip()

        # Prefer bosVersion.current; fall back to other string-ish fields.
        ver = None
        bosv = details.get("bosVersion")
        if isinstance(bosv, dict):
            cur = bosv.get("current")
            if isinstance(cur, str) and cur.strip():
                ver = cur.strip()
        if ver is None:
            for k in ("version", "bos_version", "firmwareVersion", "kernelVersion"):
                vv = details.get(k)
                if isinstance(vv, str) and vv.strip():
                    ver = vv.strip()
                    break
        if ver is not None:
            out["version"] = ver

        # Uptime seconds: prefer bosminerUptimeS, then systemUptimeS.
        up = None
        for k in ("bosminerUptimeS", "systemUptimeS", "systemUptime"):
            v = details.get(k)
            try:
                if v is not None:
                    up = int(float(v))
                    break
            except Exception:
                continue
        if up is not None and up >= 0:
            out["uptimeSeconds"] = up

        # Device model/name (nice to have)
        ident = details.get("minerIdentity")
        if isinstance(ident, dict):
            nm = ident.get("name") or ident.get("minerModel")
            if isinstance(nm, str) and nm.strip():
                out["deviceModel"] = nm.strip()

        # best-effort found blocks (rare in gRPC; keep as best effort only)
        fb = _deep_find_numbers(details, ["foundblocks", "blocksfound", "found_blocks", "found block", "blocks found"], max_hits=1)
        if fb:
            try:
                out["foundBlocks"] = int(fb[0])
            except Exception:
                pass

    # --- Pool / Stratum (PoolService/GetPoolGroups)
    pools_resp = None
    if _braiins_grpc_has_method(discovery, "braiins.bos.v1.PoolService/GetPoolGroups"):
        pools_resp = _grpc_call_json("braiins.bos.v1.PoolService/GetPoolGroups", "{}")

    if isinstance(pools_resp, dict):
        pool_groups = pools_resp.get("poolGroups")
        active_pool: Optional[Dict[str, Any]] = None

        pools_out: List[Dict[str, Any]] = []
        if isinstance(pool_groups, list):
            for g in pool_groups:
                if not isinstance(g, dict):
                    continue
                pools = g.get("pools")
                if not isinstance(pools, list):
                    continue
                for p in pools:
                    if not isinstance(p, dict):
                        continue
                    po = {
                        "url": p.get("url"),
                        "user": p.get("user"),
                        "enabled": p.get("enabled"),
                        "alive": p.get("alive"),
                        "active": p.get("active"),
                        "uid": p.get("uid"),
                        "stats": p.get("stats"),
                        "group": g.get("name") or g.get("uid"),
                    }
                    pools_out.append(po)
                    if active_pool is None and bool(p.get("active")):
                        active_pool = po

        if pools_out:
            out["pools"] = pools_out

        if active_pool:
            url = active_pool.get("url")
            user = active_pool.get("user")
            if isinstance(url, str) and url.strip():
                out["stratumURL"] = url.strip()
            if isinstance(user, str) and user.strip():
                out["stratumUser"] = user.strip()
            out["stratumAlive"] = bool(active_pool.get("alive"))
            out["stratumEnabled"] = bool(active_pool.get("enabled"))

        # Sometimes "stats" is present per pool, which can include accepted/rejected/bestShare.
        # Keep best-effort rollups (do not overwrite already-present MinerStats values unless empty).
        def _stat_int(d: Any, k: str) -> Optional[int]:
            if not isinstance(d, dict):
                return None
            v = d.get(k)
            if v is None:
                return None
            try:
                return int(float(v))
            except Exception:
                return None

        if active_pool and isinstance(active_pool.get("stats"), dict):
            st = active_pool["stats"]
            acc = _stat_int(st, "acceptedShares")
            rej = _stat_int(st, "rejectedShares")
            best = _stat_int(st, "bestShare")
            lastd = _stat_int(st, "lastDifficulty")
            lst = st.get("lastShareTime")
            if acc is not None:
                out.setdefault("sharesAccepted", acc)
            if rej is not None:
                out.setdefault("sharesRejected", rej)
            if best is not None:
                out.setdefault("bestDiff", best)
            if lastd is not None:
                out.setdefault("lastDifficulty", lastd)
            if isinstance(lst, str) and lst.strip():
                out.setdefault("lastShareTime", lst.strip())

    return out


def _poll_braiins_grpc(ip: str, timeout_s: float, cfg: Dict[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """Poll Braiins OS gRPC MinerService/GetMinerStats for rich metrics."""
    ok, token, err = _braiins_grpc_login(ip, timeout_s, cfg)
    if not ok:
        return False, None, err
    port = int(cfg.get("grpc_port") or 50051)
    try:
        def _run_get_stats(header: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [
                    "grpcurl",
                    "-plaintext",
                    "-H",
                    header,
                    "-d",
                    "{}",
                    f"{ip}:{port}",
                    "braiins.bos.v1.MinerService/GetMinerStats",
                ],
                capture_output=True,
                text=True,
                timeout=max(float(timeout_s or 1.2), 3.5),
            )

        cp = _run_get_stats(f"authorization: {token}")
        if cp.returncode != 0:
            msg = (cp.stderr or cp.stdout or "").lower()
            if ("unauth" in msg) or ("permission" in msg) or ("denied" in msg):
                cp = _run_get_stats(f"authorization: Bearer {token}")

        if cp.returncode != 0:
            return False, None, (cp.stderr or cp.stdout or "grpcurl GetMinerStats failed").strip()
        stats = json.loads(cp.stdout or "{}")

        def _get(path, default=None):
            x = stats
            for k in path:
                if not isinstance(x, dict) or k not in x:
                    return default
                x = x[k]
            return x

        g5s = _get(["minerStats", "realHashrate", "last5s", "gigahashPerSecond"])
        g1m = _get(["minerStats", "realHashrate", "last1m", "gigahashPerSecond"])
        g5m = _get(["minerStats", "realHashrate", "last5m", "gigahashPerSecond"])
        g15m = _get(["minerStats", "realHashrate", "last15m", "gigahashPerSecond"])
        g24h = _get(["minerStats", "realHashrate", "last24h", "gigahashPerSecond"])
        gavg = _get(["minerStats", "realHashrate", "sinceRestart", "gigahashPerSecond"])

        watts = _get(["powerStats", "approximatedConsumption", "watt"])
        jth = _get(["powerStats", "efficiency", "joulePerTerahash"])
        acc = _get(["poolStats", "acceptedShares"])
        rej = _get(["poolStats", "rejectedShares"])
        best = _get(["poolStats", "bestShare"])
        lastdiff = _get(["poolStats", "lastDifficulty"])
        lastshare = _get(["poolStats", "lastShareTime"])

        def to_f(v):
            try:
                return float(v)
            except Exception:
                return None

        def ghs_to_ths(v):
            v = to_f(v)
            return (v / 1000.0) if v is not None else None

        info = {
            "type": "Braiins OS (gRPC)",
            "deviceModel": "Braiins OS",
            "authRequired": True,
            "grpc_port": port,
            "hashRate": to_f(g5s),
            "hashRate_1m": to_f(g1m),
            "hashRate_5m": to_f(g5m),
            "hashRate_15m": to_f(g15m),
            "hashRate_24h": to_f(g24h),
            "hashRate_avg": to_f(gavg),
            "hashRate_THs": ghs_to_ths(g5s),
            "hashRate_1m_THs": ghs_to_ths(g1m),
            "hashRate_5m_THs": ghs_to_ths(g5m),
            "hashRate_15m_THs": ghs_to_ths(g15m),
            "hashRate_24h_THs": ghs_to_ths(g24h),
            "hashRate_avg_THs": ghs_to_ths(gavg),
            "power": to_f(watts),
            "efficiency_j_per_th": to_f(jth),
            "accepted": to_f(acc),
            "rejected": to_f(rej),
            "bestShare": to_f(best),
            "lastDifficulty": to_f(lastdiff),
            "lastShareTime": lastshare,
            "last_seen": int(time.time()),
            # aliases used by UI cards
            "hashrate": to_f(g5s),
            "hashrate_1m": to_f(g1m),
            "hashrate_5m": to_f(g5m),
            "hashrate_15m": to_f(g15m),
            "hashrate_24h": to_f(g24h),
            "hashrate_avg": to_f(gavg),
            "hashrate_ths": ghs_to_ths(g5s),
            "sharesAccepted": to_f(acc),
            "sharesRejected": to_f(rej),
            "bestDiff": to_f(best),
            "efficiency": to_f(jth),
            # will be filled by extras when available
            "temp": None,
            "boardTemp": None,
            "fanspeed": None,
            "fanrpm": None,
            "hostname": None,
            "version": None,
            "uptimeSeconds": None,
            "stratumURL": None,
            "stratumUser": None,
            "stratumAlive": None,
            "stratumEnabled": None,
            "foundBlocks": None,
            "_raw_grpc": stats,
        }

        # Optional richer metrics via extra gRPC calls.
        try:
            extra = _poll_braiins_grpc_extras(ip, port, token, timeout_s)
            for k, v in extra.items():
                if v is None:
                    continue
                info[k] = v
        except Exception as e:
            logger.debug("[braiins_grpc] extras failed ip=%s err=%s", ip, e)

        return True, info, None
    except Exception as e:
        return False, None, str(e)


def _merge_braiins_cfg(device_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge global settings defaults with per-device overrides."""
    cfg: Dict[str, Any] = dict(DEFAULT_DEVICE_CFG.get("braiins", {}))

    # merge global settings (dashboard_settings.settings_json)
    try:
        s = _get_settings()
        if isinstance(s.get("braiins"), dict):
            for k, v in s["braiins"].items():
                if v is None:
                    continue
                cfg[k] = v
    except Exception:
        pass

    # merge per-device overrides (dashboard_devices.config_json)
    if device_cfg:
        for k, v in device_cfg.items():
            if v is None:
                continue
            cfg[k] = v

    # normalize ports
    cfg["papi_port"] = int(cfg.get("papi_port") or 4028)
    cfg["grpc_port"] = int(cfg.get("grpc_port") or 50051)

    # accept common credential keys from UI / legacy configs
    if not cfg.get("grpc_username"):
        if cfg.get("username"):
            cfg["grpc_username"] = cfg.get("username")
        elif isinstance(cfg.get("auth"), dict) and cfg["auth"].get("username"):
            cfg["grpc_username"] = cfg["auth"].get("username")
    if not cfg.get("grpc_password"):
        if cfg.get("password"):
            cfg["grpc_password"] = cfg.get("password")
        elif cfg.get("pass"):
            cfg["grpc_password"] = cfg.get("pass")
        elif isinstance(cfg.get("auth"), dict) and cfg["auth"].get("password"):
            cfg["grpc_password"] = cfg["auth"].get("password")

    cfg["grpc_username"] = str(cfg.get("grpc_username") or "root")
    cfg["grpc_password"] = str(cfg.get("grpc_password") or "")

    return cfg


def _bosminer_query(ip: str, command: str, timeout_s: float, port: int = 4028, req_id: int = 1) -> Dict[str, Any]:
    """BOSminer/Braiins PAPI: JSON command over TCP (usually port 4028)."""
    payload = json.dumps({"command": command, "id": req_id}) + "\n"
    buf = b""
    with socket.create_connection((ip, int(port)), timeout=timeout_s) as sock:
        sock.settimeout(timeout_s)
        sock.sendall(payload.encode("utf-8"))
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if len(buf) > 2_000_000:
                break
    txt = buf.decode("utf-8", errors="replace").strip()
    candidates = [t for t in txt.splitlines() if t.strip()]
    if not candidates:
        raise RuntimeError("Empty response")
    last = candidates[-1]
    try:
        data = json.loads(last)
    except Exception:
        data = json.loads(txt)
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected response type")
    return data


def _probe_bosminer_papi(ip: str, timeout_s: float, cfg: Optional[Dict[str, Any]] = None) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """
    Probe BOSminer / Braiins PAPI (cgminer-compatible JSON over TCP, usually :4028).
    """
    port = int((cfg or {}).get("papi_port") or 4028)
    last_err: Optional[str] = None
    for cmd, section in (("summary", "SUMMARY"), ("stats", "STATS"), ("fans", "FANS"), ("pools", "POOLS")):
        try:
            data = _bosminer_query(ip, cmd, timeout_s, port=port, req_id=1)
            if not isinstance(data, dict) or "STATUS" not in data:
                last_err = "unexpected response"
                continue

            desc = None
            try:
                st = (data.get("STATUS") or [{}])[0]
                desc = st.get("Description") or st.get("Msg")
            except Exception:
                pass

            if section in data:
                return True, {"description": desc, "port": port, "probe_cmd": cmd}, None

            for k in ("SUMMARY", "STATS", "FANS", "POOLS", "DEVS"):
                if k in data:
                    return True, {"description": desc, "port": port, "probe_cmd": cmd}, None

            last_err = "unexpected response"
        except Exception as e:
            last_err = str(e)
            continue
    return False, None, last_err or "no response"


def _poll_bosminer_papi(ip: str, timeout_s: float, cfg: Optional[Dict[str, Any]] = None) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    port = int((cfg or {}).get("papi_port") or 4028)
    try:
        summary = _bosminer_query(ip, "summary", timeout_s, port=port, req_id=1)
        temps = _bosminer_query(ip, "temps", timeout_s, port=port, req_id=2)
        fans = _bosminer_query(ip, "fans", timeout_s, port=port, req_id=3)

        srow = None
        if isinstance(summary.get("SUMMARY"), list) and summary["SUMMARY"]:
            srow = summary["SUMMARY"][0]
        elif isinstance(summary.get("SUMMARY"), dict):
            srow = summary["SUMMARY"]
        else:
            srow = summary

        hr_gh = _first_number(srow, ["ghs_5s", "ghs", "ghs5s", "ghs_5", "ghs_15m", "ghs_av", "ghs_avg"])
        if hr_gh is None:
            mh = _first_number(srow, ["mhs 5s", "mhs_5s", "mhs5s", "mhs av", "mhs_av", "mhs_avg", "mhs"])
            if mh is not None:
                hr_gh = float(mh) / 1000.0

        power = _first_number(srow, ["power", "watts", "watt", "power_w", "power (w)"])
        if power is None:
            try:
                stats = _bosminer_query(ip, "stats", timeout_s, port=port, req_id=4)
                power = _first_number(stats, ["power", "watts", "power_w"])
            except Exception:
                power = None

        acc = _first_number(srow, ["accepted", "shares accepted", "accepted_shares"])
        rej = _first_number(srow, ["rejected", "shares rejected", "rejected_shares"])
        best = _first_number(srow, ["best share", "best_share", "bestshare", "best difficulty", "best_difficulty", "bestdiff"])

        chip_max = None
        board_max = None
        tlist = temps.get("TEMPS") if isinstance(temps, dict) else None
        if isinstance(tlist, list):
            chips = [float(t.get("Chip")) for t in tlist if isinstance(t, dict) and isinstance(t.get("Chip"), (int, float))]
            boards = [float(t.get("Board")) for t in tlist if isinstance(t, dict) and isinstance(t.get("Board"), (int, float))]
            chip_max = max(chips) if chips else None
            board_max = max(boards) if boards else None

        f_speed = None
        f_rpm = None
        flist = fans.get("FANS") if isinstance(fans, dict) else None
        if isinstance(flist, list):
            speeds = [float(f.get("Speed")) for f in flist if isinstance(f, dict) and isinstance(f.get("Speed"), (int, float))]
            rpms = [float(f.get("RPM")) for f in flist if isinstance(f, dict) and isinstance(f.get("RPM"), (int, float))]
            f_speed = sum(speeds) / len(speeds) if speeds else None
            f_rpm = sum(rpms) / len(rpms) if rpms else None

        info: Dict[str, Any] = {
            "type": "Braiins OS (BOSminer PAPI)",
            "papi_port": port,
            "hostname": None,
            "deviceModel": "BOSminer",
            "hashrate": hr_gh,
            "power": power,
            "temp": chip_max,
            "boardTemp": board_max,
            "fanspeed": f_speed,
            "fanrpm": f_rpm,
            "sharesAccepted": acc,
            "sharesRejected": rej,
            "bestDiff": best,
            "raw": {
                "summary": summary,
                "temps": temps,
                "fans": fans,
            },
        }

        return True, info, None
    except Exception as e:
        print(f"[bosminer_papi] poll failed for {ip}:{port}: {e}")
        return False, None, str(e)


# _probe_braiins_rest removed (project uses Braiins gRPC + BOSminer PAPI only)


def _braiins_get_token(ip: str, base_url: str, cfg: Dict[str, Any], timeout_s: float) -> Optional[str]:
    user = str(cfg.get("rest_username") or "").strip()
    pw = str(cfg.get("rest_password") or "").strip()
    if not user or not pw:
        return None

    now = time.time()
    cached = _BRAIINS_TOKEN_CACHE.get(ip)
    if cached and cached.get("token") and float(cached.get("expires_at") or 0) > now + 10:
        return str(cached["token"])

    url = base_url + "/api/v1/auth/login"
    try:
        r = requests.post(url, json={"username": user, "password": pw}, timeout=timeout_s, verify=False)
        if r.status_code >= 400:
            return None
        js = r.json() if r.content else {}
        token = js.get("token") or js.get("access_token") or js.get("jwt")
        ttl = js.get("timeout_s") or js.get("expires_in") or 3600
        if token:
            _BRAIINS_TOKEN_CACHE[ip] = {"token": token, "expires_at": now + float(ttl)}
            return str(token)
        return None
    except Exception:
        return None


def _braiins_get_json(ip: str, base_url: str, path: str, cfg: Dict[str, Any], timeout_s: float) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
    headers = {"Accept": "application/json"}
    token = _braiins_get_token(ip, base_url, cfg, timeout_s)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = base_url + path
    r = requests.get(url, timeout=timeout_s, verify=False, headers=headers)
    if r.status_code >= 400:
        return None, r.status_code
    try:
        js = r.json()
    except Exception:
        return None, r.status_code
    return js if isinstance(js, dict) else {"raw": js}, r.status_code


# _poll_braiins_rest removed (project uses Braiins gRPC + BOSminer PAPI only)


def _looks_like_http_miner_payload(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    ident_ok = any(k in data for k in ("deviceModel", "ASICModel", "minerModel", "model", "hwModel", "hardwareModel"))
    metric_ok = any(
        k in data
        for k in (
            "hashRate",
            "hashrate",
            "hashRate_1m",
            "hashRate_10m",
            "hashRate_1h",
            "power",
            "temp",
            "boardTemp",
            "chipTemp",
            "vrmTemp",
            "fanspeed",
            "fanrpm",
            "sharesAccepted",
            "sharesRejected",
            "bestDiff",
            "foundBlocks",
            "uptimeSeconds",
            "macAddr",
        )
    )
    return bool(ident_ok and metric_ok)


def _looks_like_supported_miner(detected: str, info: object) -> bool:
    d = (detected or "").strip().lower()
    if d in ("avalon_cgminer", "bosminer_papi", "braiins_grpc"):
        return True
    if d in ("http", "bitaxe", "nerdqaxe"):
        return _looks_like_http_miner_payload(info)
    return False


def _fetch_system_info(
    ip: str,
    timeout_s: float,
    poll_type: str = "auto",
    device_cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str], str]:
    pt = (poll_type or "auto").strip().lower()

    # Explicit Braiins OS gRPC polling (with PAPI fallback)
    if pt in ("braiins", "braiins_grpc", "grpc"):
        cfg = _merge_braiins_cfg(device_cfg)
        ok, info, err = _poll_braiins_grpc(ip, timeout_s, cfg)
        if not ok:
            ok2, info2, err2 = _poll_bosminer_papi(ip, timeout_s, cfg)
            if ok2:
                return ok2, info2, None, "bosminer_papi"
            return False, None, err or err2, "braiins_grpc"
        return ok, info, err, "braiins_grpc"

    # Explicit BOSminer/Braiins legacy PAPI polling
    if pt in ("bosminer", "bosminer_papi", "braiins_papi", "papi"):
        cfg = _merge_braiins_cfg(device_cfg)
        ok, info, err = _poll_bosminer_papi(ip, timeout_s, cfg)
        if not ok and err:
            logger.warning("BOSminer poll failed ip=%s err=%s", ip, err)
        return ok, info, err, "bosminer_papi"

    # Explicit Avalon polling
    if pt in ("avalon", "avalon_q", "cgminer", "avalon_cgminer"):
        ok, info, err = _poll_avalon_q(ip, timeout_s)
        return ok, info, err, "avalon_cgminer"

    # Explicit HTTP polling (BitAxe/NerdQAxe style)
    if pt in ("http", "bitaxe", "nerdqaxe"):
        url = f"http://{ip}/api/system/info"
        try:
            r = requests.get(url, timeout=timeout_s)
            r.raise_for_status()
            data = r.json()
            if not _looks_like_http_miner_payload(data):
                return False, None, "Not a supported miner HTTP API", "http"
            return True, data, None, "http"
        except Exception as e:
            return False, None, str(e), "http"

    # Auto-detect:
    cfg = _merge_braiins_cfg(device_cfg)
    quick = max(0.9, min(1.5, (timeout_s or 1.2) * 0.9))

    ok_papi, _meta_p, err_papi = _probe_bosminer_papi(ip, quick, cfg)
    if ok_papi:
        ok_full, info_p, err_full = _poll_bosminer_papi(ip, timeout_s, cfg)
        return ok_full, info_p, err_full, "bosminer_papi"

    ok_probe, _ver, err_a = _probe_avalon_q(ip, quick)
    if ok_probe:
        ok_full, info_a, err_full = _poll_avalon_q(ip, timeout_s)
        return ok_full, info_a, err_full, "avalon_cgminer"

    ok_grpc, _grpc_meta, err_grpc = _probe_braiins_grpc(ip, quick, cfg)
    if ok_grpc:
        ok_full, info_g, err_full = _poll_braiins_grpc(ip, timeout_s, cfg)
        if ok_full:
            return ok_full, info_g, err_full, "braiins_grpc"
        ok_p, info_p, err_p = _poll_bosminer_papi(ip, timeout_s, cfg)
        if ok_p:
            return ok_p, info_p, None, "bosminer_papi"
        return False, None, err_full or err_p or err_grpc, "braiins_grpc"

    url = f"http://{ip}/api/system/info"
    try:
        r = requests.get(url, timeout=timeout_s)
        r.raise_for_status()
        data = r.json()
        if not _looks_like_http_miner_payload(data):
            raise RuntimeError("Not a supported miner HTTP API")
        return True, data, None, "http"
    except Exception as e:
        extras = []
        if err_grpc:
            extras.append(f"rest probe: {err_grpc}")
        if err_papi:
            extras.append(f"bosminer probe: {err_papi}")
        if err_a:
            extras.append(f"avalon probe: {err_a}")
        extra = (" (" + "; ".join(extras) + ")") if extras else ""
        return False, None, str(e) + extra, "auto"


@router.get("/settings")
def api_get_settings():
    return {"settings": _get_settings()}


@router.post("/settings")
def api_update_settings(payload: SettingsUpdate):
    current = _get_settings()
    merged = _deep_merge(current, payload.settings or {})
    _save_settings(merged)
    return {"status": "ok", "settings": merged}


# ---- Network difficulty helper (for block-odds UI) ----
_DIFFICULTY_CACHE: Dict[str, Any] = {"difficulty": None, "source": None, "as_of": None, "fetched_at": 0.0}


def _fetch_difficulty_from_mempool(api_base: str = "https://mempool.space", timeout_s: float = 2.5) -> float:
    tip_hash = requests.get(f"{api_base}/api/blocks/tip/hash", timeout=timeout_s).text.strip()
    if not tip_hash:
        raise RuntimeError("Empty tip hash")
    blk = requests.get(f"{api_base}/api/block/{tip_hash}", timeout=timeout_s).json()
    diff = blk.get("difficulty")
    if diff is None:
        raise RuntimeError("No difficulty in block payload")
    return float(diff)


def _get_network_difficulty() -> Dict[str, Any]:
    best_diff: Optional[float] = None
    best_as_of: Optional[str] = None
    try:
        rows = db.get_devices()
    except Exception:
        rows = []
    for d in rows:
        info_json = d.get("last_info_json")
        if not info_json:
            continue
        try:
            info = json.loads(info_json)
        except Exception:
            continue
        diff = info.get("networkDifficulty") or info.get("difficulty")
        try:
            diff_f = float(diff)
        except Exception:
            continue
        if not (diff_f > 0):
            continue
        as_of = d.get("last_poll") or d.get("last_seen") or None
        if best_as_of is None:
            best_diff, best_as_of = diff_f, as_of
            continue
        try:
            cur_ts = datetime.fromisoformat(str(as_of).replace("Z", "+00:00")).timestamp() if as_of else 0.0
        except Exception:
            cur_ts = 0.0
        try:
            best_ts = datetime.fromisoformat(str(best_as_of).replace("Z", "+00:00")).timestamp() if best_as_of else 0.0
        except Exception:
            best_ts = 0.0
        if cur_ts >= best_ts:
            best_diff, best_as_of = diff_f, as_of

    if best_diff is not None:
        return {"difficulty": best_diff, "source": "device", "as_of": best_as_of}

    now = time.time()
    if _DIFFICULTY_CACHE.get("difficulty") is not None and (now - float(_DIFFICULTY_CACHE.get("fetched_at") or 0)) < 43200:
        return {
            "difficulty": _DIFFICULTY_CACHE["difficulty"],
            "source": _DIFFICULTY_CACHE.get("source") or "cache",
            "as_of": _DIFFICULTY_CACHE.get("as_of"),
            "cached": True,
        }

    diff = _fetch_difficulty_from_mempool("https://mempool.space")
    payload = {"difficulty": diff, "source": "mempool.space", "as_of": _utcnow_iso()}
    _DIFFICULTY_CACHE.update({**payload, "fetched_at": now})
    return payload


@router.get("/network/difficulty")
def api_get_network_difficulty():
    try:
        return _get_network_difficulty()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch difficulty: {e}")


@router.get("/devices")
def api_list_devices():
    devices = _list_devices()
    out = []
    for d in devices:
        info = None
        try:
            if d.get("last_info_json"):
                info = json.loads(d["last_info_json"])
        except Exception:
            info = None
        out.append(
            {
                "id": d["id"],
                "ip": d["ip"],
                "name": d.get("name"),
                "sort_order": d.get("sort_order", 0),
                "online": bool(d.get("online", 0)),
                "last_seen": d.get("last_seen"),
                "last_poll": d.get("last_poll"),
                "last_error": d.get("last_error"),
                "poll_type": d.get("poll_type") or "http",
                "last_info": info,
                "latest_benchmark": _get_latest_benchmark_for_ip(d["ip"]),
            }
        )
    return {"devices": out}


@router.post("/devices")
def api_add_device(payload: DeviceCreate):
    ip = _validate_ip(payload.ip)
    conn = db._get_conn()
    cur = conn.cursor()
    now = _utcnow_iso()
    cur.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_order FROM dashboard_devices;")
    next_order = int(cur.fetchone()["next_order"])

    poll_type_final = (payload.poll_type or "auto").strip().lower()
    if poll_type_final not in ("auto", "http", "avalon_cgminer", "bosminer_papi", "braiins_grpc"):
        poll_type_final = "auto"
    if poll_type_final == "auto" and (payload.grpc_username or payload.grpc_password):
        poll_type_final = "braiins_grpc"

    cfg: Dict[str, Any] = {}

    if payload.grpc_username:
        cfg["grpc_username"] = payload.grpc_username
    if payload.grpc_password:
        cfg["grpc_password"] = payload.grpc_password
    if payload.grpc_port:
        cfg["grpc_port"] = int(payload.grpc_port)
    if payload.papi_port:
        cfg["papi_port"] = int(payload.papi_port)

    try:
        dumped = payload.model_dump(exclude_none=True)  # type: ignore[attr-defined]
    except Exception:
        dumped = payload.dict(exclude_none=True)  # type: ignore[attr-defined]

    braiins_cfg = None
    cfg_obj = dumped.get("config")
    if isinstance(cfg_obj, dict):
        braiins_cfg = cfg_obj.get("braiins")

    if isinstance(braiins_cfg, dict):
        ui_user = braiins_cfg.get("grpc_username") or braiins_cfg.get("rest_username") or braiins_cfg.get("username")
        ui_pass = braiins_cfg.get("grpc_password") or braiins_cfg.get("rest_password") or braiins_cfg.get("password")
        ui_port = braiins_cfg.get("grpc_port") or braiins_cfg.get("port")

        if ui_user and not cfg.get("grpc_username"):
            cfg["grpc_username"] = str(ui_user)
        if ui_pass and not cfg.get("grpc_password"):
            cfg["grpc_password"] = str(ui_pass)
        if ui_port and not cfg.get("grpc_port"):
            try:
                cfg["grpc_port"] = int(ui_port)
            except Exception:
                pass

        if poll_type_final == "auto" and (ui_user or ui_pass):
            poll_type_final = "braiins_grpc"

    if dumped.get("rest_username") and not cfg.get("grpc_username"):
        cfg["grpc_username"] = str(dumped.get("rest_username"))
    if dumped.get("rest_password") and not cfg.get("grpc_password"):
        cfg["grpc_password"] = str(dumped.get("rest_password"))

    reserved = {"ip", "name", "poll_type", "grpc_username", "grpc_password", "grpc_port", "papi_port", "rest_username", "rest_password"}
    for k, v in (dumped or {}).items():
        if k in reserved:
            continue
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool, dict, list)):
            try:
                s = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
            except Exception:
                continue
            if len(s) <= 4096:
                cfg[k] = v

    config_json = json.dumps(cfg) if cfg else None
    try:
        cur.execute(
            """
            INSERT INTO dashboard_devices (name, ip, created_at, sort_order, poll_type, config_json)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (payload.name, ip, now, next_order, poll_type_final, config_json),
        )
        conn.commit()
    except sqlite3.IntegrityError:  # type: ignore[name-defined]
        conn.close()
        raise HTTPException(status_code=409, detail="Device already exists")

    try:
        ok_a, _ver, _err = _probe_avalon_q(ip, 0.35)
        if ok_a:
            cur.execute("UPDATE dashboard_devices SET poll_type=? WHERE ip=?;", ("avalon_cgminer", ip))
            conn.commit()
            poll_type_final = "avalon_cgminer"
    except Exception:
        pass

    if poll_type_final == "auto":
        try:
            cfg_hint = {}
            if payload.grpc_username:
                cfg_hint["grpc_username"] = payload.grpc_username
            if payload.grpc_password:
                cfg_hint["grpc_password"] = payload.grpc_password
            if payload.grpc_port:
                cfg_hint["grpc_port"] = payload.grpc_port
            ok_g, _meta, _errg = _probe_braiins_grpc(ip, 0.5, _merge_braiins_cfg(cfg_hint))
            if ok_g:
                cur.execute("UPDATE dashboard_devices SET poll_type=? WHERE ip=?;", ("braiins_grpc", ip))
                conn.commit()
                poll_type_final = "braiins_grpc"
        except Exception:
            pass

    device_id = cur.lastrowid
    conn.close()
    return {
        "status": "ok",
        "device": {"id": device_id, "ip": ip, "name": payload.name, "sort_order": next_order, "poll_type": poll_type_final},
    }


@router.delete("/devices/{device_id}")
def api_delete_device(device_id: int):
    conn = db._get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM dashboard_devices WHERE id = ?;", (device_id,))
    if cur.rowcount <= 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Device not found")
    conn.commit()
    conn.close()
    return {"status": "deleted"}


@router.post("/devices/reorder")
def api_reorder_devices(payload: ReorderPayload):
    ids = payload.device_ids or []
    conn = db._get_conn()
    cur = conn.cursor()

    if ids:
        cur.execute(
            f"SELECT id FROM dashboard_devices WHERE id IN ({','.join(['?']*len(ids))});",
            ids,
        )
        existing = {int(r["id"]) for r in cur.fetchall()}
        missing = [i for i in ids if i not in existing]
        if missing:
            conn.close()
            raise HTTPException(status_code=400, detail=f"Unknown device ids: {missing}")

    for idx, did in enumerate(ids):
        cur.execute("UPDATE dashboard_devices SET sort_order = ? WHERE id = ?;", (idx, did))

    conn.commit()
    conn.close()
    return {"status": "ok"}


@router.get("/status")
def api_poll_status(
    timeout_s: Optional[float] = Query(None, ge=0.2, le=10.0),
    parallel: int = Query(32, ge=1, le=128),
):
    settings = _get_settings()
    timeout = float(timeout_s if timeout_s is not None else settings.get("request_timeout_s", 1.2))

    devices = _list_devices()
    results: List[Dict[str, Any]] = []
    now = _utcnow_iso()

    def work(d: Dict[str, Any]) -> Dict[str, Any]:
        pt = (d.get("poll_type") or "auto")
        cfg = _parse_device_cfg(d)
        ok, info, err, detected = _fetch_system_info(d["ip"], timeout, poll_type=pt, device_cfg=cfg)
        poll_update = detected if ok and detected in ("http", "avalon_cgminer", "braiins_grpc", "bosminer_papi") else None
        _write_device_poll(d["id"], ok, info, None if ok else err, poll_type=poll_update)
        latest = _get_latest_benchmark_for_ip(d["ip"])
        return {
            "id": d["id"],
            "ip": d["ip"],
            "name": d.get("name"),
            "poll_type": (poll_update or d.get("poll_type") or "http"),
            "online": ok,
            "info": info,
            "error": err,
            "last_poll": now,
            "latest_benchmark": latest,
        }

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futures = [ex.submit(work, d) for d in devices]
        for f in as_completed(futures):
            results.append(f.result())

    order = {d["id"]: (d.get("sort_order", 0), d["id"]) for d in devices}
    results.sort(key=lambda r: order.get(r["id"], (10_000, r["id"])))

    return {"now": now, "devices": results}


@router.get("/debug")
def api_debug():
    info: Dict[str, Any] = {
        "ok": True,
        "db_path": getattr(db, "DB_PATH", None),
        "cwd": os.getcwd(),
        "base_dir": _BASE_DIR,
        "asset_root": ASSET_ROOT,
        "bg_dir": BG_DIR,
        "snd_dir": SND_DIR,
    }

    try:
        conn = db._get_conn()
        cur = conn.cursor()

        cur.execute("PRAGMA database_list;")
        info["sqlite_databases"] = [{"seq": r[0], "name": r[1], "file": r[2]} for r in cur.fetchall()]

        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
        tables = [r[0] if not hasattr(r, "keys") else r["name"] for r in cur.fetchall()]
        info["tables"] = tables

        def table_info(table: str) -> Dict[str, Any]:
            out: Dict[str, Any] = {"columns": []}
            try:
                cur.execute(f"PRAGMA table_info({table});")
                cols = []
                for row in cur.fetchall():
                    cols.append(
                        {
                            "name": row[1],
                            "type": row[2],
                            "notnull": bool(row[3]),
                            "default": row[4],
                            "pk": bool(row[5]),
                        }
                    )
                out["columns"] = cols
                cur.execute(f"SELECT COUNT(*) FROM {table};")
                out["row_count"] = int(cur.fetchone()[0])
            except Exception as e:
                out["error"] = f"{type(e).__name__}: {e}"
            return out

        for t in ("dashboard_devices", "dashboard_settings", "dashboard_assets", "benchmark_runs", "profiles"):
            if t in tables:
                info[t] = table_info(t)

        conn.close()
    except Exception as e:
        info["ok"] = False
        info["error"] = f"{type(e).__name__}: {e}"

    return info


class ScanPayload(BaseModel):
    cidr: str = Field(..., description="CIDR, e.g. 192.168.10.0/24")
    timeout_s: float = Field(0.8, ge=0.2, le=10.0)
    parallel: int = Field(64, ge=1, le=128)
    limit: int = Field(512, ge=1, le=2048)


@router.post("/scan")
def api_scan(payload: ScanPayload):
    try:
        net = ipaddress.ip_network(payload.cidr, strict=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid CIDR: {payload.cidr}") from e

    hosts = [str(h) for h in net.hosts()]
    if len(hosts) > payload.limit:
        raise HTTPException(
            status_code=400,
            detail=f"Refusing to scan {len(hosts)} hosts (limit {payload.limit}). Use a smaller CIDR or raise limit.",
        )

    found: List[Dict[str, Any]] = []
    timeout = float(payload.timeout_s)

    def probe(ip: str) -> Optional[Dict[str, Any]]:
        ok, info, err, detected = _fetch_system_info(ip, timeout, poll_type="auto")
        if not ok:
            return None
        if not _looks_like_supported_miner(detected, info):
            return None
        if isinstance(info, dict):
            hostname = info.get("hostname") or info.get("host") or None
            model = info.get("deviceModel") or info.get("ASICModel") or None
        else:
            hostname, model = None, None
        return {"ip": ip, "hostname": hostname, "model": model, "detected": detected, "info": info}

    with ThreadPoolExecutor(max_workers=int(payload.parallel)) as ex:
        futures = [ex.submit(probe, ip) for ip in hosts]
        for f in as_completed(futures):
            item = f.result()
            if item:
                found.append(item)

    found.sort(key=lambda x: x.get("ip", ""))
    return {"cidr": payload.cidr, "found": found, "count": len(found)}


def _safe_filename(original: str, content: bytes) -> str:
    h = hashlib.sha256(content).hexdigest()[:16]
    base = os.path.basename(original or "file")
    base = base.replace(" ", "_")
    root, ext = os.path.splitext(base)
    ext = (ext or "").lower()[:12]
    if ext and not re.match(r"^\.[a-z0-9]+$", ext):
        ext = ""
    return f"{root[:32]}_{h}{ext}"


@router.get("/assets")
def api_list_assets(kind: str = Query("background", pattern="^(background|sound)$")):
    conn = db._get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM dashboard_assets
        WHERE kind = ?
        ORDER BY active DESC, created_at DESC, id DESC;
        """,
        (kind,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    active_id = None
    for r in rows:
        if r.get("active"):
            active_id = r["id"]
            break
    return {"kind": kind, "assets": rows, "active_id": active_id}


@router.post("/assets/upload")
async def api_upload_asset(
    kind: str = Query("background", pattern="^(background|sound)$"),
    file: UploadFile = File(...),
):
    _ensure_dirs()
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty upload")

    max_bytes = 50 * 1024 * 1024 if kind == "background" else 10 * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(status_code=400, detail=f"File too large (limit {max_bytes} bytes)")

    mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
    out_dir = BG_DIR if kind == "background" else SND_DIR
    os.makedirs(out_dir, exist_ok=True)

    sha = hashlib.sha256(content).hexdigest()
    filename = f"{sha[:16]}_{os.path.basename(file.filename or 'asset')}".replace(" ", "_")[:90]
    path = os.path.join(out_dir, filename)

    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(content)

    conn = db._get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO dashboard_assets (kind, filename, orig_name, mime, size_bytes, created_at, active)
        VALUES (?, ?, ?, ?, ?, ?, 0);
        """,
        (kind, filename, file.filename, mime, len(content), _utcnow_iso()),
    )
    asset_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return {"status": "ok", "asset": {"id": asset_id, "kind": kind, "filename": filename, "mime": mime}}


@router.post("/assets/{asset_id}/activate")
def api_activate_asset(asset_id: int, kind: str = Query("background", pattern="^(background|sound)$")):
    conn = db._get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM dashboard_assets WHERE id = ? AND kind = ?;", (asset_id, kind))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Asset not found")

    cur.execute("UPDATE dashboard_assets SET active = 0 WHERE kind = ?;", (kind,))
    cur.execute("UPDATE dashboard_assets SET active = 1 WHERE id = ?;", (asset_id,))
    conn.commit()
    conn.close()

    s = _get_settings()
    if kind == "background":
        s["assets"]["active_background_id"] = asset_id
    else:
        s["assets"]["active_sound_id"] = asset_id
    _save_settings(s)

    return {"status": "ok", "active_id": asset_id}


@router.delete("/assets/{asset_id}")
def api_delete_asset(asset_id: int, kind: str = Query("background", pattern="^(background|sound)$")):
    conn = db._get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM dashboard_assets WHERE id = ? AND kind = ?;", (asset_id, kind))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Asset not found")

    row = dict(row)

    filename = row["filename"]
    active = bool(row.get("active"))
    cur.execute("DELETE FROM dashboard_assets WHERE id = ?;", (asset_id,))
    conn.commit()
    conn.close()

    out_dir = BG_DIR if kind == "background" else SND_DIR
    path = os.path.join(out_dir, filename)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass

    if active:
        s = _get_settings()
        if kind == "background":
            s["assets"]["active_background_id"] = None
        else:
            s["assets"]["active_sound_id"] = None
        _save_settings(s)

    return {"status": "deleted"}


@router.get("/assets/{asset_id}/file")
def api_asset_file(asset_id: int):
    conn = db._get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM dashboard_assets WHERE id = ?;", (asset_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")

    row = dict(row)

    kind = row["kind"]
    filename = row["filename"]
    out_dir = BG_DIR if kind == "background" else SND_DIR
    path = os.path.join(out_dir, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Asset file missing")
    media_type = row.get("mime") or None
    return FileResponse(path, media_type=media_type, filename=row.get("orig_name") or filename)
