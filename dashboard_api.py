
# dashboard_api.py
from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile, File, Body, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
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
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import db

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

ASSET_ROOT = os.path.join("data", "dashboard_assets")
BG_DIR = os.path.join(ASSET_ROOT, "backgrounds")
SND_DIR = os.path.join(ASSET_ROOT, "sounds")

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
    "scan_default_cidr": "192.168.1.0/24",
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

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            ip TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            last_poll TEXT,
            online INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            last_info_json TEXT
        );
        """
    )

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


# Ensure tables exist at import time (keeps app.py changes minimal).
_ensure_tables()


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
    ip: str = Field(..., description="IPv4/IPv6 address")
    name: Optional[str] = None


class SettingsUpdate(BaseModel):
    settings: Dict[str, Any]


class ReorderPayload(BaseModel):
    device_ids: List[int]


def _validate_ip(ip: str) -> str:
    try:
        return str(ipaddress.ip_address(ip.strip()))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid IP: {ip}") from e


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
            last_info_json = COALESCE(?, last_info_json)
        WHERE id = ?;
        """,
        (
            1 if online else 0,
            now,
            last_seen,
            error,
            json.dumps(info) if info is not None else None,
            device_id,
        ),
    )
    conn.commit()
    conn.close()


def _cgminer_query(ip: str, command: str, timeout_s: float, port: int = 4028) -> str:
    """
    Minimal CGMiner-style API client (Avalon Q exposes this on TCP/4028).

    The protocol is: send ASCII command (no newline required), then read until EOF.
    Responses are '|' delimited records, where each record is comma-separated.
    """
    if not command:
        raise ValueError("command must be non-empty")
    data = b""
    with socket.create_connection((ip, int(port)), timeout=float(timeout_s)) as s:
        s.settimeout(float(timeout_s))
        s.sendall(command.encode("utf-8"))
        try:
            s.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
    return data.decode("utf-8", errors="replace")


def _parse_cgminer_records(raw: str) -> List[Dict[str, Any]]:
    """
    Parse a cgminer API response into a list of dict records.
    Handles keys that contain spaces (e.g. 'MHS av') as-is.
    """
    records: List[Dict[str, Any]] = []
    for rec in [r.strip() for r in str(raw).strip().split("|") if r.strip()]:
        tokens = [t for t in rec.split(",") if t != ""]
        if not tokens:
            continue
        d: Dict[str, Any] = {}
        first = tokens[0]
        if "=" in first:
            k, v = first.split("=", 1)
            d[k] = v
            d["_record"] = k
        else:
            d["_record"] = first
        for t in tokens[1:]:
            if "=" in t:
                k, v = t.split("=", 1)
                d[k] = v
            else:
                # rarely seen, but keep it
                d.setdefault("_extra", []).append(t)
        records.append(d)
    return records


_BRACKET_KV_RE = re.compile(r"([A-Za-z0-9_ ]+?)\[(.*?)\]")


def _parse_bracket_kvs(blob: str) -> Dict[str, Any]:
    """
    Parse Avalon-style '{Key[val] Key2[val2] ...}' blobs found in estats.
    Normalizes keys by removing spaces (e.g. 'Nonce Mask' -> 'NonceMask').
    """
    out: Dict[str, Any] = {}

    def parse_val(v: str) -> Any:
        s = v.strip()
        if len(s) >= 2 and ((s[0] == s[-1] == "'") or (s[0] == s[-1] == '"')):
            s = s[1:-1]

        if s.endswith("%"):
            try:
                return float(s[:-1])
            except Exception:
                return s

        parts = [p for p in s.split() if p != ""]
        if len(parts) > 1:
            # list
            nums: List[Any] = []
            ok = True
            for p in parts:
                try:
                    nums.append(int(p))
                except Exception:
                    try:
                        nums.append(float(p))
                    except Exception:
                        ok = False
                        break
            return nums if ok else s

        # scalar
        try:
            return int(s)
        except Exception:
            try:
                return float(s)
            except Exception:
                return s

    for m in _BRACKET_KV_RE.finditer(blob or ""):
        k = (m.group(1) or "").strip().replace(" ", "")
        v = m.group(2) or ""
        if not k:
            continue
        out[k] = parse_val(v)
    return out


def _extract_brace_content(s: str) -> str:
    if not s:
        return ""
    a = s.find("{")
    b = s.rfind("}")
    if a == -1 or b == -1 or b <= a:
        return ""
    return s[a + 1 : b]


def _to_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if s.endswith("%"):
            s = s[:-1]
        return float(s)
    except Exception:
        return None


def _poll_avalon_q(ip: str, timeout_s: float) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """
    Try to query an Avalon Q via its CGMiner API (TCP/4028).
    Returns (ok, info, err).
    """
    try:
        v_raw = _cgminer_query(ip, "version", timeout_s)
        s_raw = _cgminer_query(ip, "summary", timeout_s)
        e_raw = _cgminer_query(ip, "estats", timeout_s)

        v_recs = _parse_cgminer_records(v_raw)
        s_recs = _parse_cgminer_records(s_raw)
        e_recs = _parse_cgminer_records(e_raw)

        version = next((r for r in v_recs if r.get("_record") == "VERSION"), {}) or {}
        summary = next((r for r in s_recs if r.get("_record") == "SUMMARY"), {}) or {}
        estats = next((r for r in e_recs if r.get("_record") in ("STATS", "STATS=0") or str(r.get("_record")).startswith("STATS")), {}) or {}
        # estats often uses first token 'STATS=0', which becomes key STATS; keep the whole record
        if not estats:
            estats = next((r for r in e_recs if "MM ID0:Summary" in r or "HBinfo" in r), {}) or {}

        # Pull bracket blobs out of estats (MM summary + HB info)
        mm_blob_val = None
        hb_blob_val = None
        for k, val in estats.items():
            if isinstance(val, str) and "{" in val and "}" in val and ("Summary" in k or "MM" in k):
                mm_blob_val = val
            if isinstance(val, str) and "{" in val and "}" in val and k.startswith("HBinfo"):
                hb_blob_val = val

        mm = _parse_bracket_kvs(_extract_brace_content(mm_blob_val or ""))
        hb = _parse_bracket_kvs(_extract_brace_content(hb_blob_val or ""))

        # Normalize hashrate (dashboard expects GH/s) 
        hr_now = _to_float(mm.get("GHSspd"))
        if hr_now is None:
            # summary provides MHS; convert MH/s -> GH/s by /1000
            hr_now = (_to_float(summary.get("MHS 5s")) or _to_float(summary.get("MHS av")))
            if hr_now is not None:
                hr_now = hr_now / 1000.0

        hr_1m = _to_float(summary.get("MHS 1m"))
        hr_10m = _to_float(summary.get("MHS 5m"))
        hr_1h = _to_float(summary.get("MHS 15m"))
        if hr_1m is not None:
            hr_1m = hr_1m / 1000.0
        if hr_10m is not None:
            hr_10m = hr_10m / 1000.0
        if hr_1h is not None:
            hr_1h = hr_1h / 1000.0

        # Temps + fans (dashboard expects temp/vrTemp/fanspeed/fanrpm/asicTemps) 
        temp = _to_float(mm.get("TAvg") or mm.get("HBITemp") or mm.get("HBOTemp"))
        vr_temp = _to_float(mm.get("ITemp"))
        pid_t = _to_float(mm.get("TarT"))
        fanspeed = _to_float(mm.get("FanR"))
        # FanR is already '%' without the percent sign after parsing
        fan_rpms = [mm.get("Fan1"), mm.get("Fan2"), mm.get("Fan3"), mm.get("Fan4")]
        fan_rpms_f = [int(x) for x in fan_rpms if isinstance(x, (int, float)) and x > 0]
        fanrpm = int(sum(fan_rpms_f) / len(fan_rpms_f)) if fan_rpms_f else None
        asic_temps = hb.get("PVT_T0") if isinstance(hb.get("PVT_T0"), list) else None

        # Electricals: attempt to interpret PS[] (best-effort, fields vary by firmware) 
        voltage_mv = None
        current_ma = None
        power_w = _to_float(mm.get("WU"))  # WU is not watts; keep for raw only
        ps = mm.get("PS") if isinstance(mm.get("PS"), list) else None
        if ps and len(ps) >= 5:
            vin = ps[1]
            iin = ps[2]
            pwr = ps[4]
            if isinstance(vin, (int, float)):
                # observed values like 1215 (~12.15V * 100); convert to mV
                voltage_mv = float(vin) * 10.0 if float(vin) < 2000 else float(vin)
            if isinstance(iin, (int, float)):
                # usually mA-ish already
                current_ma = float(iin)
            if isinstance(pwr, (int, float)):
                power_w = float(pwr)

        # Version-ish fields / network-y fields (used in details panel) 
        mac = version.get("MAC") or mm.get("MAC") or None
        ssid = mm.get("SSID") or None
        rssi = _to_float(mm.get("RSSI"))
        uptime_s = _to_float(summary.get("Elapsed") or estats.get("Elapsed"))

        prod = version.get("PROD") or "Avalon"
        model = version.get("MODEL") or "Q"
        dev_model = f"{prod} {model}".strip()

        ver = (
            version.get("LVERSION")
            or version.get("BVERSION")
            or mm.get("Ver")
            or version.get("CGVERSION")
            or None
        )

        # Shares / diffs
        accepted = _to_float(summary.get("Accepted"))
        rejected = _to_float(summary.get("Rejected"))
        best_share = summary.get("Best Share") or None
        found_blocks = _to_float(summary.get("Found Blocks"))

        info: Dict[str, Any] = {
            "deviceModel": dev_model,
            "ASICModel": dev_model,
            "hostip": ip,
            "hostname": None,  # Avalon doesn't always expose hostname via cgminer API
            "macAddr": mac,
            "ssid": ssid,
            "wifiRSSI": rssi,
            "wifiStatus": "OK" if (ssid or rssi is not None) else None,
            "version": ver,

            # dashboard mining fields (GH/s, W, shares) 
            "hashRate": hr_now,
            "hashRate_1m": hr_1m,
            "hashRate_10m": hr_10m,
            "hashRate_1h": hr_1h,
            "power": power_w,
            "sharesAccepted": accepted,
            "sharesRejected": rejected,
            "bestSessionDiff": best_share,
            "foundBlocks": found_blocks,

            # thermals/electrical 
            "temp": temp,
            "vrTemp": vr_temp,
            "pidTargetTemp": pid_t,
            "fanspeed": fanspeed,
            "fanrpm": fanrpm,
            "asicTemps": asic_temps,
            "voltage": voltage_mv,
            "current": current_ma,
        }

        # keep raw for troubleshooting in details drawer
        info["avalon_raw"] = {
            "version": version,
            "summary": summary,
            "estats": estats,
            "mm": mm,
            "hb": hb,
        }

        return True, info, None
    except Exception as e:
        return False, None, str(e)


def _fetch_system_info(ip: str, timeout_s: float) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """
    Primary probe for our dashboard devices:
      1) Try our normal HTTP endpoint: http://<ip>/api/system/info
      2) If that fails, fall back to Avalon Q CGMiner API on TCP/4028 (version/summary/estats)
    """
    url = f"http://{ip}/api/system/info"
    http_err: Optional[str] = None
    try:
        r = requests.get(url, timeout=timeout_s)
        if r.status_code == 200:
                                                       
            data = r.json()
            if not isinstance(data, dict):
                                                 
                return True, {"raw": data}, None
            return True, data, None
        http_err = f"HTTP {r.status_code}"
    except Exception as e:
        http_err = str(e)

    ok, info, err = _poll_avalon_q(ip, timeout_s)
    if ok:
        return True, info, None
    return False, None, err or http_err


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
    """
    Uses a public mempool/esplora-compatible REST API:
      - GET /api/blocks/tip/hash -> tip block hash
      - GET /api/block/:hash -> JSON includes "difficulty"
    """
    tip_hash = requests.get(f"{api_base}/api/blocks/tip/hash", timeout=timeout_s).text.strip()
    if not tip_hash:
        raise RuntimeError("Empty tip hash")
    blk = requests.get(f"{api_base}/api/block/{tip_hash}", timeout=timeout_s).json()
    diff = blk.get("difficulty")
    if diff is None:
        raise RuntimeError("No difficulty in block payload")
    return float(diff)

def _get_network_difficulty() -> Dict[str, Any]:
    # 1) Prefer device-provided value (Bitaxe Gamma exposes 'networkDifficulty')
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
        # keep the newest timestamp we can parse
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

    # 2) Cache (avoid hammering public APIs)
    now = time.time()
    if _DIFFICULTY_CACHE.get("difficulty") is not None and (now - float(_DIFFICULTY_CACHE.get("fetched_at") or 0)) < 43200:
        return {
            "difficulty": _DIFFICULTY_CACHE["difficulty"],
            "source": _DIFFICULTY_CACHE.get("source") or "cache",
            "as_of": _DIFFICULTY_CACHE.get("as_of"),
            "cached": True,
        }

    # 3) Public fallback
    diff = _fetch_difficulty_from_mempool("https://mempool.space")
    payload = {"difficulty": diff, "source": "mempool.space", "as_of": _utcnow_iso()}
    _DIFFICULTY_CACHE.update({**payload, "fetched_at": now})
    return payload


@router.get("/network/difficulty")
def api_get_network_difficulty():
    """
    Returns current Bitcoin network difficulty.
    Prefers miners that expose it; otherwise falls back to a public mempool/esplora endpoint.
    """
    try:
        return _get_network_difficulty()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch difficulty: {e}")



@router.get("/devices")
def api_list_devices():
    devices = _list_devices()
    # light normalization for client convenience
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
    # put at end
    cur.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_order FROM dashboard_devices;")
    next_order = int(cur.fetchone()["next_order"])
    try:
        cur.execute(
            """
            INSERT INTO dashboard_devices (name, ip, created_at, sort_order)
            VALUES (?, ?, ?, ?);
            """,
            (payload.name, ip, now, next_order),
        )
        conn.commit()
    except sqlite3.IntegrityError:  # type: ignore[name-defined]
        conn.close()
        raise HTTPException(status_code=409, detail="Device already exists")
    device_id = cur.lastrowid
    conn.close()
    return {"status": "ok", "device": {"id": device_id, "ip": ip, "name": payload.name, "sort_order": next_order}}


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

    # Validate IDs exist
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
    """
    Poll each saved device's http://<ip>/api/system/info and return aggregated status.
    Also updates the DB with last_poll/last_seen/last_info_json.
    """
    settings = _get_settings()
    timeout = float(timeout_s if timeout_s is not None else settings.get("request_timeout_s", 1.2))

    devices = _list_devices()
    results: List[Dict[str, Any]] = []
    now = _utcnow_iso()

    def work(d: Dict[str, Any]) -> Dict[str, Any]:
        ok, info, err = _fetch_system_info(d["ip"], timeout)
        _write_device_poll(d["id"], ok, info, None if ok else err)
        latest = _get_latest_benchmark_for_ip(d["ip"])
        return {
            "id": d["id"],
            "ip": d["ip"],
            "name": d.get("name"),
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

    # preserve sort order from DB
    order = {d["id"]: (d.get("sort_order", 0), d["id"]) for d in devices}
    results.sort(key=lambda r: order.get(r["id"], (10_000, r["id"])))

    return {"now": now, "devices": results}


class ScanPayload(BaseModel):
    cidr: str = Field(..., description="CIDR, e.g. 192.168.10.0/24")
    timeout_s: float = Field(0.8, ge=0.2, le=10.0)
    parallel: int = Field(64, ge=1, le=128)
    limit: int = Field(512, ge=1, le=2048)


@router.post("/scan")
def api_scan(payload: ScanPayload):
    """
    Scan a CIDR for devices that respond to /api/system/info.
    Returns a list of found systems (does NOT auto-add).
    """
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
        ok, info, err = _fetch_system_info(ip, timeout)
        if not ok:
            return None
        # attach a few convenient fields
        if isinstance(info, dict):
            hostname = info.get("hostname") or info.get("host") or None
            model = info.get("deviceModel") or info.get("ASICModel") or None
        else:
            hostname, model = None, None
        return {"ip": ip, "hostname": hostname, "model": model, "info": info}

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

    # Basic size limits (keep it sane inside a docker volume)
    max_bytes = 50 * 1024 * 1024 if kind == "background" else 10 * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(status_code=400, detail=f"File too large (limit {max_bytes} bytes)")

    mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
    out_dir = BG_DIR if kind == "background" else SND_DIR
    os.makedirs(out_dir, exist_ok=True)

    # Deduplicate by sha (same filename may differ; we hash content)
    sha = hashlib.sha256(content).hexdigest()
    filename = f"{sha[:16]}_{os.path.basename(file.filename or 'asset')}".replace(" ", "_")[:90]
    path = os.path.join(out_dir, filename)

    # If file already exists, don't rewrite
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
    # verify exists
    cur.execute("SELECT id FROM dashboard_assets WHERE id = ? AND kind = ?;", (asset_id, kind))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Asset not found")

    # deactivate others
    cur.execute("UPDATE dashboard_assets SET active = 0 WHERE kind = ?;", (kind,))
    cur.execute("UPDATE dashboard_assets SET active = 1 WHERE id = ?;", (asset_id,))
    conn.commit()
    conn.close()

    # Also store in settings for convenience
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
        # clear from settings too
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
