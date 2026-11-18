"""Bridge MQTT: discovery, stati e comandi (con broker finto e pannello simulato)."""
import asyncio, json, pathlib, struct, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "zkteco_access" / "app"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from sim_panel import SimPanel
from store import Store
from manager import Manager
from api import Api
from mqtt import MQTTClient
from ha_bridge import HABridge


async def fake_broker(pubs, conn_info):
    async def handle(reader, writer):
        async def read():
            h = (await reader.readexactly(1))[0]; mult = 1; ln = 0
            while True:
                b = (await reader.readexactly(1))[0]; ln += (b & 127) * mult
                if not b & 128: break
                mult *= 128
            return h, await reader.readexactly(ln) if ln else b""
        try:
            while True:
                h, d = await read()
                t = h >> 4
                if t == 1:
                    conn_info.append(d)
                    writer.write(b"\x20\x02\x00\x00")
                elif t == 8: writer.write(b"\x90\x03" + d[:2] + b"\x00")
                elif t == 3:
                    tl = struct.unpack("!H", d[:2])[0]
                    pubs.append((d[2:2 + tl].decode(), d[2 + tl:].decode(), h & 1))
                await writer.drain()
        except Exception:
            pass
    return await asyncio.start_server(handle, "127.0.0.1", 0)


async def scenario(tmp):
    pubs, conn = [], []
    srv = await fake_broker(pubs, conn)
    port = srv.sockets[0].getsockname()[1]
    sim = SimPanel(password="1234")
    pport = await sim.start()
    st = Store(str(tmp / "b.db"))
    pid = st.save_panel({"name": "Spa", "host": "127.0.0.1", "port": pport, "password": "1234"})
    m = Manager(st, {"poll_interval": 0.2})
    api = Api(st, m, str(tmp))
    cl = MQTTClient("127.0.0.1", port, "user", "pw", will=("zkteco_access/status", "offline"))
    br = HABridge(st, m, api, cl)
    await m.start()
    cl.start()
    for _ in range(50):
        if cl.connected and m.panels[pid].online and any(p[0].endswith("/online") and p[1] == "ON" for p in pubs): break
        await asyncio.sleep(0.1)
    # CONNECT con will + user/password
    assert b"zkteco_access/status" in conn[0] and b"user" in conn[0] and b"pw" in conn[0]
    disc = {p[0]: json.loads(p[1]) for p in pubs if p[0].startswith("homeassistant/") and p[1]}
    door = st.doors()[0]
    assert f"homeassistant/lock/zk_door{door['id']}_lock/config" in disc
    assert disc[f"homeassistant/lock/zk_door{door['id']}_lock/config"]["command_topic"] == f"zkteco_access/door/{door['id']}/set"
    assert any(p[0] == f"zkteco_access/panel/{pid}/online" and p[1] == "ON" for p in pubs)
    # comando apertura via topic
    await br._on_message(f"zkteco_access/door/{door['id']}/set", b"UNLOCK")
    assert sim.controls[-1][:3] == bytes([1, 1, 1])
    # comando JSON grant
    lid = st.save_level({"name": "Spa", "doors": [door["id"]]})
    await br._on_message("zkteco_access/cmd", json.dumps({"action": "grant", "card": "123456", "name": "Cliente", "levels": ["Spa"]}).encode())
    assert sim.data["user"][0]["CardNo"] == 123456
    await asyncio.sleep(0.2)
    res = [json.loads(p[1]) for p in pubs if p[0] == "zkteco_access/cmd/result"][-1]
    assert res["ok"] is True
    await br._on_message("zkteco_access/cmd", json.dumps({"action": "revoke", "card": "123456"}).encode())
    assert sim.data["user"] == []
    # evento -> event entity + last card
    sim.push_event(777, 0, 1, 27)
    await asyncio.sleep(0.6)
    ev = [json.loads(p[1]) for p in pubs if p[0] == "zkteco_access/event"]
    assert ev and ev[-1]["event_type"] == "unknown_card" and ev[-1]["card"] == "777"
    assert any(p[0] == "zkteco_access/last_card" and p[1] == "777" for p in pubs)
    await cl.stop(); await m.stop(); await sim.stop(); srv.close()


def test_bridge(tmp_path):
    asyncio.run(asyncio.wait_for(scenario(tmp_path), 30))
