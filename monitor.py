#!/usr/bin/env python3
"""Probes three layers (LAN gateway / ISP edge / WAN) and records every result.

Each distinct network (different gateway, MAC or SSID) becomes a new `session`
with auto-detected city / ISP labels, so probes stay attributable across moves.
"""
import ipaddress
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

INTERVAL_SEC = 5
TOPO_CHECK_EVERY_CYCLES = 12          # check for network change every ~60s
ISP_EDGE_REDISCOVER_EVERY = 360       # re-traceroute every ~30min in case ISP edge changes
PING_TIMEOUT_SEC = 2
DB_PATH = Path(__file__).parent / "netmonitor.db"
GEO_API = "http://ip-api.com/json/?fields=status,country,regionName,city,isp,as,query"
# ip-api returns `as` as a single string like "AS15169 Google LLC" — keep it
# as-is. ASN + AS name is the ground-truth ISP identifier (the city name is
# approximate and the ISP name is a marketing label; both can be disputed).
WAN_TARGETS = ["1.1.1.1", "8.8.8.8"]
RTT_RE = re.compile(r"time[=<]([\d.]+)\s*ms")
# 100.64/10 is CGNAT — still ISP infrastructure but private-ish. Treat as LAN-side
# for discovery (so we keep walking past it) only if it's the very first hop.
PRIVATE_NETS = [
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
]


def run(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def is_private_lan(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in PRIVATE_NETS) or a.is_loopback


def detect_gateway() -> tuple[str | None, str | None]:
    out = run(["route", "-n", "get", "default"])
    gw = iface = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("gateway:"):
            gw = s.split(":", 1)[1].strip()
        elif s.startswith("interface:"):
            iface = s.split(":", 1)[1].strip()
    return gw, iface


def detect_gateway_mac(gw_ip: str) -> str | None:
    out = run(["arp", "-n", gw_ip])
    m = re.search(r"at\s+([0-9a-f:]{11,17})", out)
    return m.group(1) if m else None


def detect_ssid(iface: str | None) -> str | None:
    if not iface:
        return None
    out = run(["ipconfig", "getsummary", iface])
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("SSID :"):
            return s.split(":", 1)[1].strip()
    return None


def detect_isp_edge(target: str = "1.1.1.1", max_hops: int = 6) -> str | None:
    """Return the first non-LAN hop on the path to `target`."""
    out = run(
        ["traceroute", "-n", "-w", "1", "-q", "1", "-m", str(max_hops), target],
        timeout=max_hops + 3,
    )
    for line in out.splitlines():
        m = re.match(r"\s*\d+\s+(\d{1,3}(?:\.\d{1,3}){3})", line)
        if m and not is_private_lan(m.group(1)):
            return m.group(1)
    return None


def geolocate() -> dict:
    try:
        with urllib.request.urlopen(GEO_API, timeout=5) as r:
            data = json.loads(r.read())
            if data.get("status") == "success":
                return data
    except Exception:
        pass
    return {}


def make_label(geo: dict, ssid: str | None) -> str:
    parts = []
    if geo.get("city"):
        parts.append(geo["city"])
    if geo.get("isp"):
        parts.append(geo["isp"])
    if ssid:
        parts.append(f"({ssid})")
    return " - ".join(parts) if parts else "unknown"


@dataclass
class Topology:
    gateway_ip: str | None = None
    gateway_mac: str | None = None
    interface: str | None = None
    ssid: str | None = None
    isp_edge_ip: str | None = None
    public_ip: str | None = None
    isp: str | None = None
    asn: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None
    label: str = "unknown"

    def fingerprint(self) -> tuple:
        return (self.gateway_ip, self.gateway_mac, self.ssid)


def discover() -> Topology:
    t = Topology()
    t.gateway_ip, t.interface = detect_gateway()
    if t.gateway_ip:
        t.gateway_mac = detect_gateway_mac(t.gateway_ip)
    t.ssid = detect_ssid(t.interface)
    t.isp_edge_ip = detect_isp_edge()
    geo = geolocate()
    t.public_ip = geo.get("query")
    t.isp = geo.get("isp")
    t.asn = geo.get("as")
    t.city = geo.get("city")
    t.region = geo.get("regionName")
    t.country = geo.get("country")
    t.label = make_label(geo, t.ssid)
    return t


def cheap_fingerprint() -> tuple:
    gw, iface = detect_gateway()
    mac = detect_gateway_mac(gw) if gw else None
    ssid = detect_ssid(iface)
    return (gw, mac, ssid)


def init_db(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cols_ok = False
    if "probes" in tables:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(probes)")}
        cols_ok = {"session_id", "layer"}.issubset(cols)
        if not cols_ok:
            conn.executescript("ALTER TABLE probes RENAME TO probes_legacy_v1;")
    if "sessions" in tables:
        scols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        if "asn" not in scols:
            conn.executescript("ALTER TABLE sessions ADD COLUMN asn TEXT;")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id          INTEGER PRIMARY KEY,
            start_ts    INTEGER NOT NULL,
            end_ts      INTEGER,
            gateway_ip  TEXT,
            gateway_mac TEXT,
            interface   TEXT,
            ssid        TEXT,
            isp_edge_ip TEXT,
            public_ip   TEXT,
            isp         TEXT,
            asn         TEXT,
            city        TEXT,
            region      TEXT,
            country     TEXT,
            label       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS probes (
            ts         INTEGER NOT NULL,
            session_id INTEGER NOT NULL,
            layer      TEXT    NOT NULL,
            target     TEXT    NOT NULL,
            success    INTEGER NOT NULL,
            rtt_ms     REAL
        );
        CREATE INDEX IF NOT EXISTS idx_probes_ts ON probes(ts);
        CREATE INDEX IF NOT EXISTS idx_probes_session ON probes(session_id);
        CREATE INDEX IF NOT EXISTS idx_probes_layer ON probes(layer);
        PRAGMA journal_mode=WAL;
        """
    )
    conn.commit()


def open_session(conn: sqlite3.Connection, t: Topology) -> int:
    cur = conn.execute(
        """
        INSERT INTO sessions (start_ts, gateway_ip, gateway_mac, interface, ssid,
                              isp_edge_ip, public_ip, isp, asn, city, region, country, label)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (int(time.time()), t.gateway_ip, t.gateway_mac, t.interface, t.ssid,
         t.isp_edge_ip, t.public_ip, t.isp, t.asn, t.city, t.region, t.country, t.label),
    )
    conn.commit()
    return cur.lastrowid


def close_session(conn: sqlite3.Connection, session_id: int) -> None:
    conn.execute("UPDATE sessions SET end_ts=? WHERE id=?", (int(time.time()), session_id))
    conn.commit()


def ping_once(target: str) -> tuple[int, float | None]:
    try:
        p = subprocess.run(
            ["ping", "-c", "1", "-W", str(PING_TIMEOUT_SEC * 1000), target],
            capture_output=True, text=True, timeout=PING_TIMEOUT_SEC + 1,
        )
        if p.returncode == 0:
            m = RTT_RE.search(p.stdout)
            return 1, (float(m.group(1)) if m else None)
    except Exception:
        pass
    return 0, None


def probe_all(topo: Topology, pool: ThreadPoolExecutor) -> list[tuple[str, str, int, float | None]]:
    targets: list[tuple[str, str]] = []
    if topo.gateway_ip:
        targets.append(("lan", topo.gateway_ip))
    if topo.isp_edge_ip:
        targets.append(("isp", topo.isp_edge_ip))
    for t in WAN_TARGETS:
        targets.append(("wan", t))
    results = list(pool.map(lambda x: (x[0], x[1], *ping_once(x[1])), targets))
    return results


def print_topo(session_id: int, t: Topology) -> None:
    print(f"[netmonitor] session {session_id}: {t.label}")
    print(f"  gateway={t.gateway_ip} mac={t.gateway_mac} iface={t.interface} ssid={t.ssid}")
    print(f"  isp_edge={t.isp_edge_ip} public_ip={t.public_ip} isp={t.isp} "
          f"asn={t.asn} city={t.city} region={t.region} country={t.country}", flush=True)


def close_dangling_sessions(conn: sqlite3.Connection) -> None:
    """Close any sessions left open by a previous abrupt shutdown (SIGTERM from
    launchd, kernel panic, etc.). Sets end_ts to the timestamp of the session's
    last probe, or its start_ts if no probes recorded."""
    conn.execute(
        """
        UPDATE sessions SET end_ts = COALESCE(
            (SELECT MAX(ts) FROM probes WHERE session_id = sessions.id),
            start_ts
        )
        WHERE end_ts IS NULL
        """
    )
    conn.commit()


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    close_dangling_sessions(conn)
    topo = discover()
    session_id = open_session(conn, topo)
    fp = topo.fingerprint()
    print_topo(session_id, topo)

    pool = ThreadPoolExecutor(max_workers=len(WAN_TARGETS) + 2)
    cycle = 0
    while True:
        start = time.time()
        ts = int(start)
        results = probe_all(topo, pool)
        conn.executemany(
            "INSERT INTO probes (ts, session_id, layer, target, success, rtt_ms) "
            "VALUES (?,?,?,?,?,?)",
            [(ts, session_id, layer, tgt, s, r) for (layer, tgt, s, r) in results],
        )
        conn.commit()

        layer_ok: dict[str, list[int]] = {}
        for layer, _, s, _ in results:
            layer_ok.setdefault(layer, []).append(s)
        down_layers = [l for l, ss in layer_ok.items() if not any(ss)]
        if down_layers:
            print(f"[netmonitor] {ts} DOWN layers={down_layers}", flush=True)

        cycle += 1
        if cycle % TOPO_CHECK_EVERY_CYCLES == 0:
            new_fp = cheap_fingerprint()
            if new_fp != fp:
                prev_isp = topo.isp or "unknown"
                prev_city = topo.city or "unknown"
                prev_label = topo.label
                print(f"[netmonitor] network change detected: {fp} -> {new_fp}", flush=True)
                close_session(conn, session_id)
                topo = discover()
                session_id = open_session(conn, topo)
                fp = topo.fingerprint()
                new_isp = topo.isp or "unknown"
                new_city = topo.city or "unknown"
                if prev_isp != new_isp:
                    print(
                        f"[netmonitor] *** ISP CHANGED: '{prev_isp}' ({prev_city}) "
                        f"-> '{new_isp}' ({new_city}) ***",
                        flush=True,
                    )
                elif prev_city != new_city:
                    print(
                        f"[netmonitor] *** LOCATION CHANGED (same ISP): "
                        f"'{prev_city}' -> '{new_city}' ***",
                        flush=True,
                    )
                elif prev_label != topo.label:
                    print(
                        f"[netmonitor] reconnected (same ISP/location, different SSID): "
                        f"'{prev_label}' -> '{topo.label}'",
                        flush=True,
                    )
                else:
                    print("[netmonitor] reconnected (same network identity)", flush=True)
                print_topo(session_id, topo)
            elif cycle % ISP_EDGE_REDISCOVER_EVERY == 0:
                new_edge = detect_isp_edge()
                if new_edge and new_edge != topo.isp_edge_ip:
                    print(f"[netmonitor] isp_edge {topo.isp_edge_ip} -> {new_edge}", flush=True)
                    topo.isp_edge_ip = new_edge
                    conn.execute("UPDATE sessions SET isp_edge_ip=? WHERE id=?", (new_edge, session_id))
                    conn.commit()

        elapsed = time.time() - start
        time.sleep(max(0.0, INTERVAL_SEC - elapsed))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
