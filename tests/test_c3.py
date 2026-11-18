"""Test del protocollo con frame reali catturati (fixture di zkaccess-c3, firmware C3-400 4.3.4)."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "zkteco_access" / "app"))
import c3

FULL = bytes.fromhex((pathlib.Path(__file__).resolve().parent / "fixtures" / "c3_table_cfg.hex").read_text().strip())

def test_crc_and_frames():
    # risposta di connect reale: aa00c80400 4ac70100 ee3d 55
    hdr, rest = bytes.fromhex("aa00c80400"), bytes.fromhex("4ac70100ee3d55")
    code, body = c3.parse_frame(hdr, rest)
    assert code == c3.REPLY_OK and body == bytes.fromhex("4ac70100")
    f = c3.build_frame(c3.CMD_GET_PARAM, b"LockCount")
    assert c3.parse_frame(f[:5], f[5:]) == (c3.CMD_GET_PARAM, b"LockCount")

def test_table_cfg_and_user_decode():
    code, body = c3.parse_frame(FULL[:5], FULL[5:])
    tables = c3.parse_table_cfg(body[4:])
    assert len(tables) == 13
    user = tables["user"]
    assert user.index == 1 and user.field("StartTime").index == 6 and user.field("Name").type == "s"
    data = bytes.fromhex("0109010203040506070809"
        "01010387D6120376543200010001000100000100"
        "010203a1a3a303b1b2b3000100042a893401049fb03401000100")
    rows = c3.decode_records(user, data)
    assert rows[0]["UID"] == 1 and rows[0]["CardNo"] == 1234567
    assert rows[1]["StartTime"] == 20220202 and rows[1]["EndTime"] == 20230303
    # encoder simmetrico: ricodifico gli stessi record e confronto byte per byte
    # l'encoder usa la codifica del PullSDK (0 => lunghezza 0): deve tornare identico in decodifica
    enc = c3.encode_records(user, rows)
    assert c3.decode_records(user, enc) == rows
    assert enc.startswith(bytes.fromhex("010901020304050607080901010387d612"))

def test_userauthorize_encode():
    t = c3.TableCfg("userauthorize", 2, [c3.TableField("Pin","i",1), c3.TableField("AuthorizeTimezoneId","i",2), c3.TableField("AuthorizeDoorId","i",3)])
    enc = c3.encode_records(t, [{"Pin": "101", "AuthorizeTimezoneId": 1, "AuthorizeDoorId": 15}])
    assert enc == bytes([2, 3, 1, 2, 3, 1, 101, 1, 1, 1, 15])
    enc = c3.encode_records(t, [{"Pin": 70000}])
    assert enc == bytes([2, 1, 1, 3]) + (70000).to_bytes(3, "little")

def test_datetime_and_tz():
    from datetime import datetime
    d = datetime(2017, 7, 30, 16, 51, 49)
    assert c3.decode_datetime(c3.encode_datetime(d)) == d
    assert c3.decode_datetime(0x21ADADA5) == d
    assert c3.encode_tz_interval("00:00", "23:59") == 2359
    assert c3.decode_tz_interval(c3.encode_tz_interval("08:30", "18:00")) == ("08:30", "18:00")

def test_rtlog_binary():
    status = bytes([3,0,0,0, 2,0,0,0, 1,1,255,0, 0xFC,0x31,0xB3,0x21])
    ev = (1234567).to_bytes(4,"little") + (101).to_bytes(4,"little") + bytes([4, 2, 0, 0]) + (0x21ADADA5).to_bytes(4,"little")
    log = c3.parse_rtlog_binary(status + ev)
    assert log.status.sensors[0] == 2 and log.status.alarms[0] == 3
    e = log.events[0]
    assert (e.card, e.pin, e.door, e.event, e.verify) == (1234567, "101", 2, 0, 4)

def test_rtlog_kv():
    txt = "time=2024-01-02 10:11:12\tpin=0\tcardno=555\teventaddr=1\tevent=27\tinoutstatus=0\tverifytype=4\tindex=3\r\ntime=2024-01-02 10:11:13\tsensor=09\trelay=01\talarm=01100000\r\n"
    log = c3.parse_rtlog_kv(txt)
    assert log.events[0].card == 555 and log.events[0].event == 27
    assert log.status.sensors == [1, 2, 0, 0] and log.status.alarms == [1, 0x10, 0, 0]
