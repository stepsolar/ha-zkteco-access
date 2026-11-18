"""Importazione della configurazione dal database di ZKAccess 3.5 (zkaccess.mdb).

Importa: dispositivi (IP, porta, password), nomi porte, ingressi/uscite ausiliari,
fasce orarie, festività, livelli di accesso con le loro porte, utenti/tessere con
reparto (camera) e livelli assegnati.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from jet4 import DB
from store import Store

_LOGGER = logging.getLogger("zkteco.import")
DAYS = {"sunday": "sun", "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu",
        "friday": "fri", "saturday": "sat", "holidaytype1": "hol1", "holidaytype2": "hol2", "holidaytype3": "hol3"}


def _rows(db: DB, table: str) -> list[dict[str, Any]]:
    try:
        return [r for r in db.rows(table) if r]
    except KeyError:
        return []


def _hhmm(value: Any) -> str:
    if not value:
        return "00:00"
    s = str(value)
    if " " in s:
        s = s.split(" ", 1)[1]
    return s[:5]


def _date(value: Any) -> str:
    if not value:
        return ""
    s = str(value)[:10]
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return ""
    return "" if d.year < 2000 else s


def inspect(path: str) -> dict:
    db = DB(path)
    return {
        "dispositivi": len(_rows(db, "Machines")),
        "porte": len(_rows(db, "acc_door")),
        "livelli": len(_rows(db, "acc_levelset")),
        "utenti": len(_rows(db, "USERINFO")),
    }


def import_mdb(path: str, store: Store, replace: bool = False) -> dict:
    db = DB(path)
    machines = [m for m in _rows(db, "Machines") if m.get("IP")]
    if not machines and not _rows(db, "USERINFO"):
        raise ValueError("Il file non sembra un database ZKAccess (tabelle Machines/USERINFO assenti)")

    stats = {"dispositivi": 0, "porte": 0, "livelli": 0, "utenti": 0, "tessere": 0, "fasce_orarie": 0}
    with store.tx():
        if replace:
            for t in ("user_levels", "user_doors", "users", "level_doors", "levels", "doors", "aux", "panels", "holidays", "sync_queue"):
                store.x(f"DELETE FROM {t}")
            store.x("DELETE FROM timezones WHERE id<>1")

        # ---- dispositivi
        panel_map: dict[int, int] = {}
        for m in machines:
            name = (m.get("MachineAlias") or f"Pannello {m['ID']}").strip()
            ex = store.q1("SELECT id FROM panels WHERE host=? AND port=?", (m["IP"], m.get("Port") or 4370))
            locks = int(m.get("door_count") or 0) or (4 if (m.get("acpanel_type") or 4) == 4 else int(m.get("acpanel_type") or 4))
            args = (name, m["IP"], int(m.get("Port") or 4370), m.get("CommPassword") or "", 1 if m.get("Enabled", True) else 0,
                    m.get("sn") or "", m.get("FirmwareVersion") or "", locks,
                    int(m.get("aux_in_count") or 4), int(m.get("aux_out_count") or 4))
            if ex:
                store.x("""UPDATE panels SET name=?,host=?,port=?,password=?,enabled=?,serial=?,firmware=?,locks=?,aux_in=?,aux_out=?
                           WHERE id=?""", (*args, ex["id"]))
                pid = ex["id"]
            else:
                pid = store.x("""INSERT INTO panels(name,host,port,password,enabled,serial,firmware,locks,aux_in,aux_out)
                                 VALUES(?,?,?,?,?,?,?,?,?,?)""", args)
            panel_map[m["ID"]] = pid
            stats["dispositivi"] += 1

        # ---- porte
        door_map: dict[int, int] = {}
        for d in _rows(db, "acc_door"):
            pid = panel_map.get(d.get("device_id"))
            if not pid or not d.get("door_no"):
                continue
            name = (d.get("door_name") or f"Porta {d['door_no']}").strip()
            secs = int(d.get("lock_delay") or 5)
            store.x("""INSERT INTO doors(panel_id,door_no,name,open_seconds) VALUES(?,?,?,?)
                       ON CONFLICT(panel_id,door_no) DO UPDATE SET name=excluded.name, open_seconds=excluded.open_seconds""",
                    (pid, d["door_no"], name, secs))
            door_map[d["id"]] = store.q1("SELECT id FROM doors WHERE panel_id=? AND door_no=?", (pid, d["door_no"]))["id"]
            stats["porte"] += 1

        # ---- ausiliari
        for a in _rows(db, "acc_auxiliary"):
            pid = panel_map.get(a.get("device_id"))
            if not pid or not a.get("aux_no"):
                continue
            kind = "out" if "Output" in (a.get("printer_number") or "") else "in"
            store.x("""INSERT INTO aux(panel_id,kind,aux_no,name) VALUES(?,?,?,?)
                       ON CONFLICT(panel_id,kind,aux_no) DO UPDATE SET name=excluded.name""",
                    (pid, kind, a["aux_no"], (a.get("aux_name") or "").strip() or f"Aux {a['aux_no']}"))

        # ---- fasce orarie (l'id ZKAccess diventa TimezoneId)
        tz_map: dict[int, int] = {}
        for t in _rows(db, "acc_timeseg"):
            data = {}
            for long, short in DAYS.items():
                data[short] = [[_hhmm(t.get(f"{long}_start{i}")), _hhmm(t.get(f"{long}_end{i}"))] for i in (1, 2, 3)]
            tid = int(t["id"])
            store.x("""INSERT INTO timezones(id,name,data) VALUES(?,?,?)
                       ON CONFLICT(id) DO UPDATE SET name=excluded.name, data=excluded.data""",
                    (tid, (t.get("timeseg_name") or f"Fascia {tid}").strip(), json.dumps(data)))
            tz_map[tid] = tid
            stats["fasce_orarie"] += 1

        for h in _rows(db, "acc_holidays"):
            day = _date(h.get("start_date"))
            if day:
                store.x("INSERT INTO holidays(name,day,type,loop) VALUES(?,?,?,?)",
                        ((h.get("holiday_name") or "Festività").strip(), day, int(h.get("holiday_type") or 1),
                         1 if int(h.get("loop_by_year") or 2) == 1 else 0))

        # ---- livelli
        level_map: dict[int, int] = {}
        for lv in _rows(db, "acc_levelset"):
            name = (lv.get("level_name") or f"Livello {lv['id']}").strip()
            tz = tz_map.get(int(lv.get("level_timeseg_id") or 1), 1)
            ex = store.q1("SELECT id FROM levels WHERE name=?", (name,))
            if ex:
                lid = ex["id"]
                store.x("UPDATE levels SET timezone_id=? WHERE id=?", (tz, lid))
            else:
                lid = store.x("INSERT INTO levels(name,timezone_id) VALUES(?,?)", (name, tz))
            level_map[lv["id"]] = lid
            stats["livelli"] += 1
        for g in _rows(db, "acc_levelset_door_group"):
            lid, did = level_map.get(g.get("acclevelset_id")), door_map.get(g.get("accdoor_id"))
            if lid and did:
                store.x("INSERT OR IGNORE INTO level_doors VALUES(?,?)", (lid, did))

        # ---- utenti
        depts = {r["DEPTID"]: (r.get("DEPTNAME") or "").strip() for r in _rows(db, "DEPARTMENTS")}
        user_map: dict[int, int] = {}
        for u in _rows(db, "USERINFO"):
            pin = str(u.get("Badgenumber") or "").strip()
            if not pin.isdigit():
                continue
            card = str(u.get("CardNo") or "").strip()
            card = card if card.isdigit() and card != "0" else ""
            name = " ".join(x for x in ((u.get("name") or "").strip(), (u.get("lastname") or "").strip()) if x)
            grp = depts.get(u.get("DEFAULTDEPTID"), "")
            if grp == depts.get(1):
                grp = ""
            ex = store.q1("SELECT id FROM users WHERE pin=?", (pin,))
            if card:
                clash = store.q1("SELECT id FROM users WHERE card=? AND pin<>?", (card, pin))
                if clash:
                    store.x("UPDATE users SET card='' WHERE id=?", (clash["id"],))
            vals = (name, card, str(u.get("PASSWORD") or "").strip(), grp,
                    _date(u.get("acc_startdate")), _date(u.get("acc_enddate")),
                    0 if u.get("isblacklist") else 1)
            if ex:
                store.x("UPDATE users SET name=?,card=?,password=?,grp=?,start_date=?,end_date=?,enabled=? WHERE id=?", (*vals, ex["id"]))
                uid = ex["id"]
            else:
                uid = store.x("""INSERT INTO users(pin,name,card,password,grp,start_date,end_date,enabled)
                                 VALUES(?,?,?,?,?,?,?,?)""", (pin, *vals))
            user_map[u["USERID"]] = uid
            stats["utenti"] += 1
            stats["tessere"] += 1 if card else 0
        for e in _rows(db, "acc_levelset_emp"):
            uid, lid = user_map.get(e.get("employee_id")), level_map.get(e.get("acclevelset_id"))
            if uid and lid:
                store.x("INSERT OR IGNORE INTO user_levels VALUES(?,?)", (uid, lid))
    return stats
