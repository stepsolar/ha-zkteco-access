"""Archivio SQLite dell'add-on (/data/zkteco.db)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS panels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 4370,
    password TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    serial TEXT DEFAULT '', firmware TEXT DEFAULT '', model TEXT DEFAULT '',
    locks INTEGER DEFAULT 4, aux_in INTEGER DEFAULT 4, aux_out INTEGER DEFAULT 4,
    last_seen REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS doors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id INTEGER NOT NULL REFERENCES panels(id) ON DELETE CASCADE,
    door_no INTEGER NOT NULL,
    name TEXT NOT NULL,
    open_seconds INTEGER NOT NULL DEFAULT 5,
    hidden INTEGER NOT NULL DEFAULT 0,
    UNIQUE(panel_id, door_no)
);
CREATE TABLE IF NOT EXISTS aux (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id INTEGER NOT NULL REFERENCES panels(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,          -- in / out
    aux_no INTEGER NOT NULL,
    name TEXT NOT NULL,
    hidden INTEGER NOT NULL DEFAULT 0,
    UNIQUE(panel_id, kind, aux_no)
);
CREATE TABLE IF NOT EXISTS timezones (
    id INTEGER PRIMARY KEY,      -- = TimezoneId sui pannelli
    name TEXT NOT NULL,
    data TEXT NOT NULL           -- json {"sun":[["00:00","23:59"],...], ... "hol1":..}
);
CREATE TABLE IF NOT EXISTS holidays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    day TEXT NOT NULL,           -- YYYY-MM-DD
    type INTEGER NOT NULL DEFAULT 1,
    loop INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS levels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    timezone_id INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS level_doors (
    level_id INTEGER NOT NULL REFERENCES levels(id) ON DELETE CASCADE,
    door_id INTEGER NOT NULL REFERENCES doors(id) ON DELETE CASCADE,
    PRIMARY KEY(level_id, door_id)
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pin TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    card TEXT NOT NULL DEFAULT '',
    password TEXT NOT NULL DEFAULT '',
    grp TEXT NOT NULL DEFAULT '',
    start_date TEXT NOT NULL DEFAULT '',
    end_date TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    super INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    updated REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_levels (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    level_id INTEGER NOT NULL REFERENCES levels(id) ON DELETE CASCADE,
    PRIMARY KEY(user_id, level_id)
);
CREATE TABLE IF NOT EXISTS user_doors (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    door_id INTEGER NOT NULL REFERENCES doors(id) ON DELETE CASCADE,
    PRIMARY KEY(user_id, door_id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    panel_id INTEGER, panel TEXT, door_no INTEGER, door TEXT,
    event INTEGER, description TEXT, card TEXT, pin TEXT, user TEXT,
    inout INTEGER, verify INTEGER, category TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS sync_queue (
    panel_id INTEGER NOT NULL,
    pin TEXT NOT NULL,
    PRIMARY KEY(panel_id, pin)
);
"""

DEFAULT_TZ = {d: [["00:00", "23:59"], ["00:00", "00:00"], ["00:00", "00:00"]]
              for d in ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "hol1", "hol2", "hol3")}


class Store:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            if not self.q1("SELECT id FROM timezones WHERE id=1"):
                self.x("INSERT INTO timezones(id,name,data) VALUES(1,'24 ore',?)", (json.dumps(DEFAULT_TZ),))

    # ----------------------------------------------------------- base
    def q(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def q1(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        r = self.q(sql, args)
        return r[0] if r else None

    def x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            return cur.lastrowid

    def tx(self):
        return _Tx(self)

    # -------------------------------------------------------- settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        r = self.q1("SELECT v FROM settings WHERE k=?", (key,))
        return json.loads(r["v"]) if r else default

    def set_setting(self, key: str, value: Any) -> None:
        self.x("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, json.dumps(value)))

    # ---------------------------------------------------------- pannelli
    def panels(self) -> list[dict]:
        return self.q("SELECT * FROM panels ORDER BY name")

    def panel(self, pid: int) -> dict | None:
        return self.q1("SELECT * FROM panels WHERE id=?", (pid,))

    def save_panel(self, data: dict) -> int:
        fields = ("name", "host", "port", "password", "enabled")
        vals = [data.get(f) for f in fields]
        vals[2] = int(vals[2] or 4370)
        vals[3] = vals[3] or ""
        vals[4] = 1 if vals[4] in (None, True, 1, "1", "true") else 0
        if data.get("id"):
            self.x("UPDATE panels SET name=?,host=?,port=?,password=?,enabled=? WHERE id=?", (*vals, int(data["id"])))
            pid = int(data["id"])
        else:
            pid = self.x("INSERT INTO panels(name,host,port,password,enabled) VALUES(?,?,?,?,?)", tuple(vals))
        self.ensure_ports(pid, int(data.get("locks") or self.panel(pid)["locks"] or 4))
        return pid

    def ensure_ports(self, pid: int, locks: int, aux_in: int = 4, aux_out: int = 4) -> None:
        p = self.panel(pid)
        for n in range(1, locks + 1):
            self.x("INSERT OR IGNORE INTO doors(panel_id,door_no,name) VALUES(?,?,?)", (pid, n, f"{p['name']} - Porta {n}"))
        for n in range(1, aux_in + 1):
            self.x("INSERT OR IGNORE INTO aux(panel_id,kind,aux_no,name) VALUES(?,?,?,?)", (pid, "in", n, f"{p['name']} - Ingresso aux {n}"))
        for n in range(1, aux_out + 1):
            self.x("INSERT OR IGNORE INTO aux(panel_id,kind,aux_no,name) VALUES(?,?,?,?)", (pid, "out", n, f"{p['name']} - Uscita aux {n}"))

    def update_panel_info(self, pid: int, **kw: Any) -> None:
        if not kw:
            return
        cols = ",".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE panels SET {cols} WHERE id=?", (*kw.values(), pid))

    # ------------------------------------------------------------- porte
    def doors(self) -> list[dict]:
        return self.q("""SELECT d.*, p.name AS panel_name FROM doors d JOIN panels p ON p.id=d.panel_id
                         WHERE d.door_no <= p.locks ORDER BY p.name, d.door_no""")

    def door(self, did: int) -> dict | None:
        return self.q1("SELECT d.*, p.name AS panel_name FROM doors d JOIN panels p ON p.id=d.panel_id WHERE d.id=?", (did,))

    def aux_list(self) -> list[dict]:
        return self.q("""SELECT a.*, p.name AS panel_name FROM aux a JOIN panels p ON p.id=a.panel_id
                         WHERE (a.kind='in' AND a.aux_no<=p.aux_in) OR (a.kind='out' AND a.aux_no<=p.aux_out)
                         ORDER BY p.name, a.kind, a.aux_no""")

    # ------------------------------------------------------------ livelli
    def levels(self) -> list[dict]:
        lv = self.q("SELECT * FROM levels ORDER BY name")
        links = self.q("SELECT * FROM level_doors")
        counts = {r["level_id"]: r["n"] for r in self.q("SELECT level_id, COUNT(*) n FROM user_levels GROUP BY level_id")}
        for l in lv:
            l["doors"] = [r["door_id"] for r in links if r["level_id"] == l["id"]]
            l["users"] = counts.get(l["id"], 0)
        return lv

    def save_level(self, data: dict) -> int:
        name = (data.get("name") or "").strip()
        tz = int(data.get("timezone_id") or 1)
        with self.tx():
            if data.get("id"):
                lid = int(data["id"])
                self.x("UPDATE levels SET name=?, timezone_id=? WHERE id=?", (name, tz, lid))
            else:
                lid = self.x("INSERT INTO levels(name,timezone_id) VALUES(?,?)", (name, tz))
            self.x("DELETE FROM level_doors WHERE level_id=?", (lid,))
            for d in data.get("doors") or []:
                self.x("INSERT OR IGNORE INTO level_doors VALUES(?,?)", (lid, int(d)))
        return lid

    # ------------------------------------------------------------- utenti
    def users(self, search: str = "") -> list[dict]:
        args: tuple = ()
        where = ""
        if search:
            where = "WHERE pin LIKE ? OR name LIKE ? OR card LIKE ? OR grp LIKE ?"
            s = f"%{search}%"
            args = (s, s, s, s)
        us = self.q(f"SELECT * FROM users {where} ORDER BY grp, CAST(pin AS INTEGER), pin", args)
        ul = self.q("SELECT * FROM user_levels")
        ud = self.q("SELECT * FROM user_doors")
        lv: dict[int, list[int]] = {}
        dd: dict[int, list[int]] = {}
        for r in ul:
            lv.setdefault(r["user_id"], []).append(r["level_id"])
        for r in ud:
            dd.setdefault(r["user_id"], []).append(r["door_id"])
        for u in us:
            u["levels"] = lv.get(u["id"], [])
            u["doors"] = dd.get(u["id"], [])
        return us

    def user(self, uid: int | None = None, pin: str | None = None, card: str | None = None) -> dict | None:
        if uid is not None:
            u = self.q1("SELECT * FROM users WHERE id=?", (uid,))
        elif pin is not None:
            u = self.q1("SELECT * FROM users WHERE pin=?", (str(pin),))
        else:
            u = self.q1("SELECT * FROM users WHERE card=? AND card<>''", (str(card),))
        if u:
            u["levels"] = [r["level_id"] for r in self.q("SELECT level_id FROM user_levels WHERE user_id=?", (u["id"],))]
            u["doors"] = [r["door_id"] for r in self.q("SELECT door_id FROM user_doors WHERE user_id=?", (u["id"],))]
        return u

    def next_pin(self) -> str:
        r = self.q1("SELECT MAX(CAST(pin AS INTEGER)) m FROM users WHERE pin GLOB '[0-9]*'")
        return str(max(int(r["m"] or 0) + 1, 1))

    def save_user(self, data: dict) -> int:
        pin = str(data.get("pin") or "").strip() or self.next_pin()
        if not pin.isdigit():
            raise ValueError("Il PIN (n. utente) deve essere numerico")
        card = str(data.get("card") or "").strip()
        if card and not card.isdigit():
            raise ValueError("Il numero tessera deve essere numerico")
        if card:
            other = self.q1("SELECT id,pin FROM users WHERE card=? AND id<>?", (card, int(data.get("id") or 0)))
            if other:
                raise ValueError(f"Tessera già assegnata all'utente {other['pin']}")
        vals = (pin, data.get("name") or "", card, str(data.get("password") or ""), data.get("grp") or "",
                data.get("start_date") or "", data.get("end_date") or "",
                0 if data.get("enabled") in (False, 0, "0", "false") else 1,
                1 if data.get("super") in (True, 1, "1", "true") else 0,
                data.get("notes") or "", time.time())
        with self.tx():
            if data.get("id"):
                uid = int(data["id"])
                self.x("""UPDATE users SET pin=?,name=?,card=?,password=?,grp=?,start_date=?,end_date=?,enabled=?,super=?,notes=?,updated=?
                          WHERE id=?""", (*vals, uid))
            else:
                if self.q1("SELECT id FROM users WHERE pin=?", (pin,)):
                    raise ValueError(f"Il PIN {pin} esiste già")
                uid = self.x("""INSERT INTO users(pin,name,card,password,grp,start_date,end_date,enabled,super,notes,updated)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", vals)
            if "levels" in data:
                self.x("DELETE FROM user_levels WHERE user_id=?", (uid,))
                for l in data.get("levels") or []:
                    self.x("INSERT OR IGNORE INTO user_levels VALUES(?,?)", (uid, int(l)))
            if "doors" in data:
                self.x("DELETE FROM user_doors WHERE user_id=?", (uid,))
                for d in data.get("doors") or []:
                    self.x("INSERT OR IGNORE INTO user_doors VALUES(?,?)", (uid, int(d)))
        return uid

    def user_door_ids(self, uid: int) -> dict[int, int]:
        """door_id -> timezone_id per un utente (livelli + porte dirette)."""
        out: dict[int, int] = {}
        for r in self.q("""SELECT ld.door_id, l.timezone_id FROM user_levels ul
                           JOIN levels l ON l.id=ul.level_id JOIN level_doors ld ON ld.level_id=l.id
                           WHERE ul.user_id=?""", (uid,)):
            out.setdefault(r["door_id"], r["timezone_id"])
        for r in self.q("SELECT door_id FROM user_doors WHERE user_id=?", (uid,)):
            out.setdefault(r["door_id"], 1)
        return out

    # ------------------------------------------------------------ eventi
    def add_event(self, ev: dict) -> int:
        cols = ("ts", "panel_id", "panel", "door_no", "door", "event", "description", "card", "pin", "user", "inout", "verify", "category")
        return self.x(f"INSERT INTO events({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(ev.get(c) for c in cols))

    def events(self, limit: int = 200, before: float | None = None, search: str = "", panel_id: int | None = None,
               category: str = "", since: float | None = None) -> list[dict]:
        where, args = [], []
        if before:
            where.append("ts < ?"); args.append(before)
        if since:
            where.append("ts >= ?"); args.append(since)
        if panel_id:
            where.append("panel_id = ?"); args.append(panel_id)
        if category:
            where.append("category = ?"); args.append(category)
        if search:
            where.append("(door LIKE ? OR user LIKE ? OR card LIKE ? OR pin LIKE ? OR description LIKE ?)")
            args += [f"%{search}%"] * 5
        w = ("WHERE " + " AND ".join(where)) if where else ""
        return self.q(f"SELECT * FROM events {w} ORDER BY ts DESC, id DESC LIMIT ?", (*args, int(limit)))

    def purge_events(self, days: int) -> None:
        if days > 0:
            self.x("DELETE FROM events WHERE ts < ?", (time.time() - days * 86400,))

    # ------------------------------------------------------- coda sync
    def queue(self, panel_id: int, pin: str) -> None:
        self.x("INSERT OR IGNORE INTO sync_queue VALUES(?,?)", (panel_id, str(pin)))

    def queued(self, panel_id: int) -> list[str]:
        return [r["pin"] for r in self.q("SELECT pin FROM sync_queue WHERE panel_id=?", (panel_id,))]

    def dequeue(self, panel_id: int, pin: str) -> None:
        self.x("DELETE FROM sync_queue WHERE panel_id=? AND pin=?", (panel_id, str(pin)))

    def queue_count(self) -> dict[int, int]:
        return {r["panel_id"]: r["n"] for r in self.q("SELECT panel_id, COUNT(*) n FROM sync_queue GROUP BY panel_id")}


class _Tx:
    def __init__(self, store: Store) -> None:
        self.s = store

    def __enter__(self):
        self.s._lock.acquire()
        self.s._db.execute("BEGIN")
        return self

    def __exit__(self, et, ev, tb):
        try:
            self.s._db.execute("ROLLBACK" if et else "COMMIT")
        finally:
            self.s._lock.release()
        return False
