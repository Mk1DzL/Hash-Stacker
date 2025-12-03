# db.py
from __future__ import annotations

import os
import json
import sqlite3
from typing import Any, Dict, List, Optional
from datetime import datetime

DB_PATH = os.path.join("data", "benchmarks.db")


def _get_conn() -> sqlite3.Connection:
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS benchmark_runs (
            id TEXT PRIMARY KEY,
            bitaxe_ip TEXT NOT NULL,
            device_type TEXT NOT NULL,
            status TEXT NOT NULL,
            profile_name TEXT,
            notes TEXT,
            started_at TEXT,
            finished_at TEXT,
            best_hashrate REAL,
            best_efficiency REAL,
            result_path TEXT,
            error_reason TEXT,
            config_json TEXT NOT NULL
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            device_type TEXT NOT NULL,
            config_json TEXT NOT NULL
        );
        """
    )

    conn.commit()
    conn.close()


def create_run(
    run_id: str,
    bitaxe_ip: str,
    device_type: str,
    status: str,
    profile_name: Optional[str],
    notes: str,
    config: Dict[str, Any],
) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    started = datetime.utcnow().isoformat()
    cur.execute(
        """
        INSERT INTO benchmark_runs (
            id, bitaxe_ip, device_type, status, profile_name, notes,
            started_at, config_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            run_id,
            bitaxe_ip,
            device_type,
            status,
            profile_name,
            notes,
            started,
            json.dumps(config),
        ),
    )
    conn.commit()
    conn.close()


def finish_run(
    run_id: str,
    status: str,
    best_hashrate: Optional[float],
    best_efficiency: Optional[float],
    result_path: Optional[str],
    error_reason: Optional[str],
) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    finished = datetime.utcnow().isoformat()
    cur.execute(
        """
        UPDATE benchmark_runs
        SET status = ?, finished_at = ?, best_hashrate = ?, best_efficiency = ?,
            result_path = ?, error_reason = ?
        WHERE id = ?;
        """,
        (
            status,
            finished,
            best_hashrate,
            best_efficiency,
            result_path,
            error_reason,
            run_id,
        ),
    )
    conn.commit()
    conn.close()


def update_run_status(run_id: str, status: str, error_reason: Optional[str] = None) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE benchmark_runs
        SET status = ?, error_reason = ?
        WHERE id = ?;
        """,
        (status, error_reason, run_id),
    )
    conn.commit()
    conn.close()


def list_runs() -> List[Dict[str, Any]]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM benchmark_runs
        ORDER BY started_at DESC;
        """
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM benchmark_runs WHERE id = ?;", (run_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def save_profile(name: str, device_type: str, config: Dict[str, Any]) -> int:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO profiles (name, device_type, config_json)
        VALUES (?, ?, ?);
        """,
        (name, device_type, json.dumps(config)),
    )
    profile_id = cur.lastrowid
    conn.commit()
    conn.close()
    return profile_id


def delete_profile(profile_id: int) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM profiles WHERE id = ?;", (profile_id,))
    conn.commit()
    conn.close()


def list_profiles() -> List[Dict[str, Any]]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, name, device_type, config_json
        FROM profiles
        ORDER BY name ASC;
        """
    )
    rows = cur.fetchall()
    conn.close()
    profiles: List[Dict[str, Any]] = []
    for r in rows:
        profiles.append(
            {
                "id": f"custom:{r['id']}",
                "name": r["name"],
                "deviceType": r["device_type"],
                "builtin": False,
                "config": json.loads(r["config_json"]),
            }
        )
    return profiles
