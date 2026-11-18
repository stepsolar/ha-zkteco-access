import asyncio, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "zkteco_access" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from sim_panel import SimPanel
from store import Store
from manager import Manager

async def scenario(tmp):
    a, b = SimPanel(password="75357535"), SimPanel(password="", session=False)
    pa, pb = await a.start(), await b.start()
    st = Store(str(tmp / "z.db"))
    ida = st.save_panel({"name": "Door 101 to 104", "host": "127.0.0.1", "port": pa, "password": "75357535"})
    idb = st.save_panel({"name": "Cancello", "host": "127.0.0.1", "port": pb, "password": ""})
    m = Manager(st, {"poll_interval": 0.2})
    events = []
    m.subscribe(lambda k, d: events.append((k, d)))
    await m.start()
    for _ in range(50):
        if all(rt.online for rt in m.panels.values()): break
        await asyncio.sleep(0.1)
    assert all(rt.online for rt in m.panels.values()), [rt.error for rt in m.panels.values()]
    assert m.panels[idb].client.session_less and not m.panels[ida].client.session_less
    da = {d["door_no"]: d["id"] for d in st.doors() if d["panel_id"] == ida}
    db_ = {d["door_no"]: d["id"] for d in st.doors() if d["panel_id"] == idb}
    lid_room = st.save_level({"name": "101", "doors": [da[1]]})
    lid_gate = st.save_level({"name": "Cancello", "doors": [db_[1], db_[2]]})
    uid = st.save_user({"pin": "201", "card": "2095868724", "name": "Ospite", "grp": "101",
                        "end_date": "2026-12-31", "levels": [lid_room, lid_gate], "doors": [da[3]]})
    res = await m.push_user("201")
    assert set(res.values()) == {"ok"}, res
    ua = {r["AuthorizeTimezoneId"]: r["AuthorizeDoorId"] for r in a.data["userauthorize"]}
    assert ua == {1: 0b101}, a.data
    assert a.data["user"][0]["CardNo"] == 2095868724 and a.data["user"][0]["EndTime"] == 20261231
    assert b.data["userauthorize"][0]["AuthorizeDoorId"] == 0b11
    # tolgo il livello cancello -> l'utente sparisce dal pannello B
    u = st.user(uid); u["levels"] = [lid_room]; st.save_user(u)
    await m.push_user("201")
    assert b.data["user"] == [] and b.data["userauthorize"] == []
    # evento tessera + learn
    learn = asyncio.ensure_future(m.learn_card(5))
    await asyncio.sleep(0.05)
    a.push_event(2095868724, 201, 1, 0)
    a.push_event(999, 0, 2, 27)
    got = await learn
    assert got["card"] == "2095868724" and got["known_user"] == "201", got
    await asyncio.sleep(0.5)
    evs = st.events()
    assert any(e["event"] == 27 and e["card"] == "999" for e in evs)
    assert any(e["user"].startswith("Ospite") for e in evs)
    # apertura remota + stato
    await m.open_door(da[2], 3)
    assert a.controls[-1] == bytes([1, 2, 1, 3, 0])
    assert m.panel_state(ida)["doors"][2]["unlocked"]
    # parametri porta
    p = await m.get_door_params(da[1]); assert p["Drivertime"] == "5"
    await m.set_door_params(da[1], {"Drivertime": 7}); assert a.params["Door1Drivertime"] == "7"
    # full sync rimuove utenti sconosciuti
    a.data["user"].append({"Pin": 999, "CardNo": 1}); a.data["userauthorize"].append({"Pin": 999, "AuthorizeTimezoneId": 1, "AuthorizeDoorId": 1})
    r = await m.full_sync(ida)
    assert r == {"inviati": 1, "rimossi": 1}, r
    assert [x["Pin"] for x in a.data["user"]] == [201]
    assert any(t["TimezoneId"] == 1 and t["SunTime1"] == 2359 for t in a.data["timezone"])
    # import dai pannelli
    st.x("DELETE FROM users")
    r = await m.import_from_panels(); assert r["aggiunti"] == 1, r
    assert sorted(st.user_door_ids(st.user(pin="201")["id"])) == sorted([da[1], da[3]])
    # pannello offline -> coda
    await b.stop()
    await m.panel_action(idb, "reconnect")
    await asyncio.sleep(0.5)
    u = st.user(pin="201"); u["doors"] = u["doors"] + [db_[4]]; st.save_user(u)
    res = await m.push_user("201")
    assert "coda" in res["Cancello"], res
    assert st.queued(idb) == ["201"]
    await m.stop()
    await a.stop()

def test_manager(tmp_path):
    asyncio.run(scenario(tmp_path))
