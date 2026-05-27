#!/usr/bin/env python3
"""Dashboard.

Two logical zones in the UI, served from one /api/status response:

1. CURRENT NETWORK — always reflects the network you are connected to *right now*.
   Shows session details + live per-layer pills + uptime stats for the selected
   window scoped to the current physical network (gateway MAC + SSID).

2. HISTORICAL ANALYSIS — driven by the Window dropdown + a multi-select of
   networks (city — ISP labels). Heatmap, RTT chart, and outage log all reflect
   whatever networks the user has checked.
"""
import datetime
import sqlite3
import time
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

DB_PATH = Path(__file__).parent / "netmonitor.db"
INTERVAL_SEC = 5
OUTAGE_MERGE_GAP_SEC = INTERVAL_SEC * 3
LAYERS = ["lan", "isp", "wan", "web"]
LAYER_TITLES = {
    "lan": "LAN (machine ↔ router)",
    "isp": "ISP edge (router ↔ ISP)",
    "wan": "WAN (ISP ↔ internet)",
    "web": "Web (DNS + HTTPS)",
}

app = Flask(__name__)


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def network_label(row: dict | sqlite3.Row) -> str:
    """Public label for a session = "city — isp" (or fallback)."""
    city = row["city"] if "city" in row.keys() else None
    isp = row["isp"] if "isp" in row.keys() else None
    if city and isp:
        return f"{city} — {isp}"
    return row["label"] or "unknown"


def get_current_session() -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM sessions ORDER BY start_ts DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def get_sessions_overlapping(since: int, until: int) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            """
            SELECT * FROM sessions
            WHERE start_ts <= ? AND (end_ts IS NULL OR end_ts >= ?)
            ORDER BY start_ts DESC
            """,
            (until, since),
        ).fetchall()
    return [dict(r) for r in rows]


def session_ids_for_same_network(current: dict, since: int, until: int) -> list[int]:
    """All session IDs in window sharing the current session's physical-network
    identity (gateway MAC + SSID). Stitches across daemon restarts."""
    if not current:
        return []
    with conn() as c:
        rows = c.execute(
            """
            SELECT id FROM sessions
            WHERE COALESCE(gateway_mac,'') = COALESCE(?,'')
              AND COALESCE(ssid,'')        = COALESCE(?,'')
              AND start_ts <= ? AND (end_ts IS NULL OR end_ts >= ?)
            ORDER BY start_ts
            """,
            (current["gateway_mac"], current["ssid"], until, since),
        ).fetchall()
    return [r["id"] for r in rows]


def get_available_networks(since: int, until: int) -> list[dict]:
    """Distinct network labels with data in the window, with a sample count."""
    with conn() as c:
        rows = c.execute(
            """
            SELECT
                COALESCE(s.city || ' — ' || s.isp, s.label) AS label,
                COUNT(p.ts) AS samples,
                MIN(p.ts) AS first_ts,
                MAX(p.ts) AS last_ts
            FROM sessions s LEFT JOIN probes p ON p.session_id = s.id
            WHERE s.start_ts <= ? AND (s.end_ts IS NULL OR s.end_ts >= ?)
              AND p.ts >= ? AND p.ts <= ?
            GROUP BY label
            ORDER BY last_ts DESC
            """,
            (until, since, since, until),
        ).fetchall()
    return [dict(r) for r in rows]


def session_ids_for_labels(labels: list[str], since: int, until: int) -> list[int]:
    """Resolve a list of "city — isp" labels back to overlapping session IDs."""
    if not labels:
        return []
    placeholders = ",".join("?" * len(labels))
    with conn() as c:
        rows = c.execute(
            f"""
            SELECT id FROM sessions
            WHERE COALESCE(city || ' — ' || isp, label) IN ({placeholders})
              AND start_ts <= ? AND (end_ts IS NULL OR end_ts >= ?)
            """,
            (*labels, until, since),
        ).fetchall()
    return [r["id"] for r in rows]


def per_layer_per_second(since: int, until: int, session_ids: list[int]) -> dict:
    if not session_ids:
        return {l: [] for l in LAYERS}
    placeholders = ",".join("?" * len(session_ids))
    sql = f"""
        SELECT ts, layer, SUM(success) AS ok, COUNT(*) AS total,
               AVG(CASE WHEN success=1 THEN rtt_ms END) AS avg_rtt
        FROM probes
        WHERE ts >= ? AND ts <= ? AND session_id IN ({placeholders})
        GROUP BY ts, layer ORDER BY ts
    """
    out: dict[str, list[tuple]] = {l: [] for l in LAYERS}
    with conn() as c:
        for r in c.execute(sql, (since, until, *session_ids)):
            out.setdefault(r["layer"], []).append((r["ts"], r["ok"], r["total"], r["avg_rtt"]))
    return out


def wan_rtt_bucketed_by_label(session_ids: list[int], since: int, until: int,
                              bucket_sec: int) -> list[dict]:
    """One series per (city — ISP) label, bucketed to the same interval as the
    heatmap so both charts share data-point granularity."""
    if not session_ids:
        return []
    placeholders = ",".join("?" * len(session_ids))
    sql = f"""
        SELECT (p.ts - ?) / ? AS bucket_idx,
               AVG(CASE WHEN p.success=1 THEN p.rtt_ms END) AS rtt,
               COALESCE(s.city || ' — ' || s.isp, s.label) AS label
        FROM probes p JOIN sessions s ON s.id = p.session_id
        WHERE p.layer = 'wan' AND p.session_id IN ({placeholders})
          AND p.ts >= ? AND p.ts <= ?
        GROUP BY bucket_idx, label
        ORDER BY bucket_idx
    """
    grouped: dict[str, dict] = {}
    with conn() as c:
        for r in c.execute(sql, (since, bucket_sec, *session_ids, since, until)):
            label = r["label"] or "unknown"
            bucket_ts = since + r["bucket_idx"] * bucket_sec
            g = grouped.setdefault(label, {"label": label, "points": []})
            g["points"].append({"ts": bucket_ts, "rtt": r["rtt"]})
    return list(grouped.values())


def compute_layer_outages(series: list[tuple]) -> list[dict]:
    """Emit contiguous events with status 'down' (all targets failed) or
    'degraded' (some but not all targets failed). State changes close the
    current event and open a new one — so a brownout that flips between
    partial and full loss surfaces as multiple events, not one merged blob."""
    events: list[dict] = []
    cur_s = cur_e = None
    cur_status: str | None = None

    def flush():
        nonlocal cur_s, cur_e, cur_status
        if cur_s is not None:
            events.append({"start": cur_s, "end": cur_e,
                           "duration_sec": cur_e - cur_s + INTERVAL_SEC,
                           "status": cur_status})
            cur_s = cur_e = cur_status = None

    for ts, ok, total, _rtt in series:
        if ok == 0:
            status = "down"
        elif total and ok < total:
            status = "degraded"
        else:
            status = None
        if status is None:
            flush()
            continue
        if cur_s is None:
            cur_s = cur_e = ts
            cur_status = status
        elif status == cur_status and ts - cur_e <= OUTAGE_MERGE_GAP_SEC:
            cur_e = ts
        else:
            flush()
            cur_s = cur_e = ts
            cur_status = status
    flush()
    return events


TARGET_FRIENDLY = {
    "1.1.1.1": "Cloudflare (1.1.1.1)",
    "8.8.8.8": "Google (8.8.8.8)",
    "https://www.cloudflare.com/": "Cloudflare HTTPS",
    "https://www.google.com/": "Google HTTPS",
}


def _target_label(t: str) -> str:
    return TARGET_FRIENDLY.get(t, t)


def per_target_in_window(since: int, until: int,
                         session_ids: list[int]) -> dict[tuple, list[tuple]]:
    """Return {(layer, target): [(ts, success), ...]} for the window. Used to
    enrich outage attribution with per-target evidence."""
    out: dict[tuple, list[tuple]] = {}
    if not session_ids:
        return out
    placeholders = ",".join("?" * len(session_ids))
    sql = f"""
        SELECT ts, layer, target, success FROM probes
        WHERE ts >= ? AND ts <= ? AND session_id IN ({placeholders})
        ORDER BY ts
    """
    with conn() as c:
        for r in c.execute(sql, (since, until, *session_ids)):
            out.setdefault((r["layer"], r["target"]), []).append(
                (r["ts"], r["success"]))
    return out


def _overlaps(a: dict, b: dict, slack: int = INTERVAL_SEC) -> bool:
    return a["start"] - slack <= b["end"] and b["start"] - slack <= a["end"]


def _concurrent_layers(o: dict, layer_outages: dict,
                       exclude: str) -> list[str]:
    return [l for l in LAYERS if l != exclude
            and any(_overlaps(o, e) for e in layer_outages.get(l, []))]


def _per_target_stats(layer: str, o: dict,
                      target_series: dict[tuple, list[tuple]]) -> list[dict]:
    stats = []
    for (l, t), series in target_series.items():
        if l != layer:
            continue
        slice_ = [s for ts, s in series if o["start"] <= ts <= o["end"]]
        if not slice_:
            continue
        fail = sum(1 for s in slice_ if not s)
        ok = len(slice_) - fail
        stats.append({
            "target": t, "label": _target_label(t),
            "fail": fail, "ok": ok, "n": len(slice_),
            "ratio": fail / len(slice_),
        })
    stats.sort(key=lambda s: s["target"])
    return stats


def _evidence_line(stats: list[dict], concurrent: list[str]) -> str:
    parts = [f"{s['label']} {s['fail']}/{s['n']} fail" for s in stats]
    if concurrent:
        parts.append("concurrent: " + "+".join(sorted(concurrent)))
    return " · ".join(parts)


def explain_outage(layer: str, o: dict,
                   target_series: dict[tuple, list[tuple]],
                   layer_outages: dict) -> dict:
    """Returns {verdict, summary, evidence} — a per-outage human explanation
    grounded in the actual probes that failed and what other layers did at
    the same time."""
    stats = _per_target_stats(layer, o, target_series)
    fully_failed = [s for s in stats if s["ratio"] >= 0.999]
    fully_ok = [s for s in stats if s["ratio"] == 0]
    partial = [s for s in stats if 0 < s["ratio"] < 0.999]
    concurrent = _concurrent_layers(o, layer_outages, exclude=layer)
    evidence = _evidence_line(stats, concurrent)
    target_ip = stats[0]["target"] if stats else None

    if layer == "lan":
        return {
            "verdict": "Router (LAN)",
            "summary": (f"Router {target_ip} unreachable — local network is "
                        "down. Check router power, cable, and Wi-Fi."),
            "evidence": evidence,
        }

    if layer == "isp":
        if "wan" in concurrent:
            return {
                "verdict": "ISP last-mile",
                "summary": (f"Your gateway is up but the ISP edge "
                            f"({target_ip}) and upstream are both unreachable. "
                            "Call the ISP."),
                "evidence": evidence,
            }
        return {
            "verdict": "ISP edge blip",
            "summary": (f"ISP edge {target_ip} unreachable while upstream "
                        "still routable — transient route issue on the next hop."),
            "evidence": evidence,
        }

    if layer == "wan":
        if "isp" in concurrent and o["status"] == "down":
            return {
                "verdict": "ISP last-mile",
                "summary": ("Full upstream outage — ISP edge also unreachable; "
                            "root cause is ISP last-mile, not transit."),
                "evidence": evidence,
            }
        if o["status"] == "down" and fully_failed and not fully_ok:
            names = ", ".join(s["label"] for s in fully_failed)
            return {
                "verdict": "Transit / peering",
                "summary": (f"{names} both unreachable while your ISP edge is "
                            "up — peering or route failure beyond your ISP."),
                "evidence": evidence,
            }
        if o["status"] == "degraded":
            return {
                "verdict": "Partial transit",
                "summary": ("Some upstream targets failing while others "
                            "respond — single-path or peering loss, not a full "
                            "outage."),
                "evidence": evidence,
            }
        return {
            "verdict": "Upstream affected",
            "summary": "Upstream connectivity impaired.",
            "evidence": evidence,
        }

    if layer == "web":
        if "wan" in concurrent:
            return {
                "verdict": "Upstream down",
                "summary": ("HTTPS naturally failing because upstream transit "
                            "was also unreachable at the same time."),
                "evidence": evidence,
            }
        if fully_failed and fully_ok:
            fnames = ", ".join(s["label"] for s in fully_failed)
            onames = ", ".join(s["label"] for s in fully_ok)
            return {
                "verdict": "Destination-specific",
                "summary": (f"{fnames} failing while {onames} OK — problem at "
                            "that destination, not your network."),
                "evidence": evidence,
            }
        if fully_failed and not fully_ok:
            return {
                "verdict": "DNS / TLS layer",
                "summary": ("ICMP upstream is OK but HTTPS to multiple sites "
                            "is failing — suspect DNS resolver, TLS, or captive "
                            "portal interception."),
                "evidence": evidence,
            }
        if partial:
            return {
                "verdict": "Partial HTTPS loss",
                "summary": "Intermittent HTTPS failures across web targets.",
                "evidence": evidence,
            }
        return {
            "verdict": "Web layer issue",
            "summary": "HTTPS reachability impaired.",
            "evidence": evidence,
        }

    return {"verdict": "Connectivity", "summary": "Connectivity issue.",
            "evidence": evidence}


def attribute_outages(layer_outages: dict[str, list[dict]],
                      since: int, until: int,
                      session_ids: list[int]) -> list[dict]:
    target_series = per_target_in_window(since, until, session_ids)
    flat = []
    for layer in LAYERS:
        for o in layer_outages.get(layer, []):
            exp = explain_outage(layer, o, target_series, layer_outages)
            flat.append({
                **o, "layer": layer,
                "verdict": exp["verdict"],
                "summary": exp["summary"],
                "evidence": exp["evidence"],
                "attribution": exp["summary"],
            })
    flat.sort(key=lambda x: x["start"])
    return flat


def bucket_down_ratio(series: list[tuple], since: int, until: int,
                      bucket_sec: int) -> list[float | None]:
    """Fraction of probes in each bucket that failed. Partial loss (e.g. one
    of two WAN targets down) now contributes proportionally — the heatmap
    previously only lit up when *every* target failed simultaneously."""
    n = max(1, (until - since) // bucket_sec)
    probes = [0] * n
    failed = [0] * n
    for ts, ok, total, _r in series:
        i = (ts - since) // bucket_sec
        if 0 <= i < n and total:
            probes[i] += total
            failed[i] += (total - ok)
    return [(failed[i] / probes[i]) if probes[i] else None for i in range(n)]


def bucket_times(since: int, until: int, bucket_sec: int) -> list[int]:
    n = max(1, (until - since) // bucket_sec)
    return [since + i * bucket_sec for i in range(n)]


def compute_stats(per_layer: dict, layer_outages: dict) -> dict:
    stats = {}
    for l in LAYERS:
        s = per_layer.get(l, [])
        if not s:
            stats[l] = {"uptime_pct": None, "outages": 0, "degraded_events": 0,
                        "degraded_pct": 0, "longest_sec": 0, "samples": 0}
            continue
        down_secs = sum(1 for _ts, ok, _t, _r in s if ok == 0)
        degraded_secs = sum(1 for _ts, ok, t, _r in s if t and 0 < ok < t)
        outs = layer_outages.get(l, [])
        down_events = [o for o in outs if o.get("status") == "down"]
        degraded_events = [o for o in outs if o.get("status") == "degraded"]
        stats[l] = {
            "uptime_pct": round((1 - down_secs / len(s)) * 100, 3),
            "degraded_pct": round(degraded_secs / len(s) * 100, 3),
            "outages": len(down_events),
            "degraded_events": len(degraded_events),
            "longest_sec": max((o["duration_sec"] for o in down_events), default=0),
            "samples": len(s),
        }
    return stats


def live_status_per_layer(current_session_ids: list[int]) -> dict:
    """UP/DOWN/STALE for each layer from the most recent probe on the current
    network. Independent of the historical window."""
    status = {}
    if not current_session_ids:
        return {l: "unknown" for l in LAYERS}
    placeholders = ",".join("?" * len(current_session_ids))
    with conn() as c:
        for l in LAYERS:
            r = c.execute(
                f"""
                SELECT ts, SUM(success) AS ok, COUNT(*) AS total FROM probes
                WHERE layer = ? AND session_id IN ({placeholders})
                GROUP BY ts ORDER BY ts DESC LIMIT 1
                """,
                (l, *current_session_ids),
            ).fetchone()
            if not r:
                status[l] = "unknown"
            elif time.time() - r["ts"] > INTERVAL_SEC * 3:
                status[l] = "stale"
            elif r["ok"] == 0:
                status[l] = "down"
            elif r["total"] and r["ok"] < r["total"]:
                status[l] = "degraded"
            else:
                status[l] = "up"
    return status


def quality_per_layer(since: int, until: int, session_ids: list[int]) -> dict:
    """p50/p95/p99 RTT, jitter (stddev), packet-loss %, sample count per layer."""
    if not session_ids:
        return {l: None for l in LAYERS}
    placeholders = ",".join("?" * len(session_ids))
    out = {}
    with conn() as c:
        for layer in LAYERS:
            rows = c.execute(
                f"""
                SELECT success, rtt_ms FROM probes
                WHERE layer=? AND session_id IN ({placeholders})
                  AND ts >= ? AND ts <= ?
                """,
                (layer, *session_ids, since, until),
            ).fetchall()
            if not rows:
                out[layer] = None
                continue
            total = len(rows)
            ok = sum(r["success"] for r in rows)
            rtts = sorted(r["rtt_ms"] for r in rows if r["rtt_ms"] is not None)
            if rtts:
                n = len(rtts)
                p50 = rtts[n // 2]
                p95 = rtts[min(n - 1, int(n * 0.95))]
                p99 = rtts[min(n - 1, int(n * 0.99))]
                mean = sum(rtts) / n
                jitter = (sum((x - mean) ** 2 for x in rtts) / n) ** 0.5
            else:
                p50 = p95 = p99 = jitter = None
            out[layer] = {
                "samples": total,
                "loss_pct": round((1 - ok / total) * 100, 3),
                "p50_ms": round(p50, 2) if p50 is not None else None,
                "p95_ms": round(p95, 2) if p95 is not None else None,
                "p99_ms": round(p99, 2) if p99 is not None else None,
                "jitter_ms": round(jitter, 2) if jitter is not None else None,
            }
    return out


def isp_attributed_downtime_sec(per_layer: dict) -> int:
    """Seconds in window where LAN was up but ISP / WAN / WEB was down → ISP fault."""
    lan = {ts: ok for ts, ok, _, _ in per_layer.get("lan", [])}
    isp = {ts: ok for ts, ok, _, _ in per_layer.get("isp", [])}
    wan = {ts: ok for ts, ok, _, _ in per_layer.get("wan", [])}
    web = {ts: ok for ts, ok, _, _ in per_layer.get("web", [])}
    all_ts = set(lan) | set(isp) | set(wan) | set(web)
    down_secs = 0
    for ts in all_ts:
        # If LAN didn't probe at this ts, assume LAN up (don't falsely blame ISP).
        lan_down = ts in lan and lan[ts] == 0
        if lan_down:
            continue
        isp_down = ts in isp and isp[ts] == 0
        wan_down = ts in wan and wan[ts] == 0
        web_down = ts in web and web[ts] == 0
        if isp_down or wan_down or web_down:
            down_secs += INTERVAL_SEC
    return down_secs


DAYS_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def pattern_insights(since: int, until: int, session_ids: list[int]) -> dict:
    """Surface outage patterns as short human-readable strings, NOT as a chart.
    Operates on the same `since`/`until` as the rest of the section so the
    user's Window selection drives it. Returns coverage info too so the UI
    can say "based on N hours of data".
    """
    out = {
        "insights": [],
        "covered_sec": 0,
        "total_outage_sec": 0,
        "peak_hour": None,
        "peak_hour_pct": None,
        "peak_day": None,
        "peak_day_pct": None,
    }
    if not session_ids:
        out["insights"].append("No data for this network in the selected window.")
        return out

    grid = [[0] * 24 for _ in range(7)]
    placeholders = ",".join("?" * len(session_ids))
    sql = f"""
        SELECT ts, layer, SUM(success) AS ok
        FROM probes
        WHERE ts >= ? AND ts <= ? AND session_id IN ({placeholders})
        GROUP BY ts, layer
    """
    by_ts: dict[int, dict] = {}
    with conn() as c:
        for r in c.execute(sql, (since, until, *session_ids)):
            by_ts.setdefault(r["ts"], {})[r["layer"]] = r["ok"]

    covered_secs = len(by_ts) * INTERVAL_SEC
    total = 0
    for ts, layers in by_ts.items():
        lan_down = layers.get("lan") == 0
        isp_down = layers.get("isp") == 0
        wan_down = layers.get("wan") == 0
        web_down = layers.get("web") == 0
        if not lan_down and (isp_down or wan_down or web_down):
            dt = datetime.datetime.fromtimestamp(ts)
            grid[dt.weekday()][dt.hour] += INTERVAL_SEC
            total += INTERVAL_SEC

    out["covered_sec"] = covered_secs
    out["total_outage_sec"] = total

    if covered_secs < 600:
        out["insights"].append("Not enough data in the selected window to detect a pattern (need at least 10 minutes covered).")
        return out
    if total == 0:
        out["insights"].append("No ISP-attributed outages in the selected window — connection has been stable.")
        return out

    hour_totals = [sum(grid[d][h] for d in range(7)) for h in range(24)]
    day_totals = [sum(grid[d]) for d in range(7)]
    peak_hour = max(range(24), key=lambda h: hour_totals[h])
    peak_day = max(range(7), key=lambda d: day_totals[d])
    peak_hour_pct = (hour_totals[peak_hour] / total) * 100 if total else 0
    peak_day_pct = (day_totals[peak_day] / total) * 100 if total else 0

    out["peak_hour"] = peak_hour
    out["peak_hour_pct"] = round(peak_hour_pct, 1)
    out["peak_day"] = DAYS_SHORT[peak_day]
    out["peak_day_pct"] = round(peak_day_pct, 1)

    insights = []
    span_days = (until - since) / 86400
    if peak_hour_pct >= 25:
        insights.append(
            f"Outages cluster around {peak_hour:02d}:00–{(peak_hour+1)%24:02d}:00 — "
            f"{peak_hour_pct:.0f}% of all outage time falls in that hour."
        )
    if span_days >= 2 and peak_day_pct >= 30:
        insights.append(
            f"{DAYS_SHORT[peak_day]} sees the most outages — {peak_day_pct:.0f}% of outage time."
        )

    # Coarse day-part bucket
    night = sum(hour_totals[0:6]) + sum(hour_totals[22:24])
    morning = sum(hour_totals[6:12])
    afternoon = sum(hour_totals[12:18])
    evening = sum(hour_totals[18:22])
    parts = sorted(
        [("late night/early morning (00–06, 22–24)", night),
         ("morning (06–12)", morning),
         ("afternoon (12–18)", afternoon),
         ("evening (18–22)", evening)],
        key=lambda x: -x[1],
    )
    if parts[0][1] > total * 0.4 and not insights:
        insights.append(
            f"Most outage time falls in the {parts[0][0]} ({(parts[0][1]/total)*100:.0f}%)."
        )

    if not insights:
        insights.append("Outages are spread across the day — no strong time-of-day pattern in this window.")

    out["insights"] = insights
    return out


def mtbf_and_streak(per_layer: dict, layer_outages: dict, until: int) -> dict:
    """Mean time between ISP-attributed failures + current uptime streak."""
    isp_outs = sorted(
        [o for layer in ("isp", "wan") for o in layer_outages.get(layer, [])],
        key=lambda x: x["start"],
    )
    # MTBF: average gap between end of one and start of next.
    gaps = [isp_outs[i]["start"] - isp_outs[i - 1]["end"] for i in range(1, len(isp_outs))]
    mtbf = sum(gaps) / len(gaps) if gaps else None
    streak = until - max((o["end"] for o in isp_outs), default=until)
    return {
        "mtbf_sec": round(mtbf) if mtbf else None,
        "streak_sec": max(0, streak),
        "isp_outage_count": len(isp_outs),
        "last_outage_at": isp_outs[-1]["end"] if isp_outs else None,
    }


def sparkline_rtt(session_ids: list[int], last_n_sec: int = 300) -> dict:
    """Recent per-layer RTT (avg per second) for mini-charts in cards."""
    until = int(time.time())
    since = until - last_n_sec
    out = {l: [] for l in LAYERS}
    if not session_ids:
        return out
    placeholders = ",".join("?" * len(session_ids))
    with conn() as c:
        for r in c.execute(
            f"""
            SELECT ts, layer, AVG(CASE WHEN success=1 THEN rtt_ms END) AS rtt
            FROM probes
            WHERE ts >= ? AND ts <= ? AND session_id IN ({placeholders})
            GROUP BY ts, layer ORDER BY ts
            """,
            (since, until, *session_ids),
        ):
            out.setdefault(r["layer"], []).append({"ts": r["ts"], "rtt": r["rtt"]})
    return out


def baseline_rtt(session_ids: list[int], days: int = 7) -> dict:
    """Long-window median RTT per layer for "X× baseline" tags."""
    until = int(time.time())
    since = until - days * 86400
    out = {l: None for l in LAYERS}
    if not session_ids:
        return out
    placeholders = ",".join("?" * len(session_ids))
    with conn() as c:
        for layer in LAYERS:
            rows = c.execute(
                f"""
                SELECT rtt_ms FROM probes
                WHERE layer=? AND success=1 AND rtt_ms IS NOT NULL
                  AND session_id IN ({placeholders})
                  AND ts >= ? AND ts <= ?
                """,
                (layer, *session_ids, since, until),
            ).fetchall()
            if rows:
                rtts = sorted(r["rtt_ms"] for r in rows)
                out[layer] = round(rtts[len(rtts) // 2], 2)
    return out


def monitor_health() -> dict:
    now = int(time.time())
    with conn() as c:
        last = c.execute("SELECT MAX(ts) AS ts FROM probes").fetchone()
        recent = c.execute(
            "SELECT COUNT(*) AS n FROM probes WHERE ts > ?", (now - 60,)
        ).fetchone()
        oldest = c.execute("SELECT MIN(ts) AS ts FROM probes").fetchone()
        sessions_n = c.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
    db_bytes = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    wal_bytes = (DB_PATH.parent / "netmonitor.db-wal").stat().st_size \
        if (DB_PATH.parent / "netmonitor.db-wal").exists() else 0
    last_ts = last["ts"]
    oldest_ts = oldest["ts"]
    return {
        "last_probe_ts": last_ts,
        "last_probe_age_sec": now - last_ts if last_ts else None,
        "probes_last_60s": recent["n"],
        "expected_probes_per_60s": (60 // INTERVAL_SEC) * 4,  # 4 targets per cycle
        "db_bytes": db_bytes,
        "wal_bytes": wal_bytes,
        "oldest_probe_ts": oldest_ts,
        "retention_days": 56,
        "retention_used_pct": round(((now - oldest_ts) / (56 * 86400)) * 100, 1)
            if oldest_ts else 0,
        "sessions_total": sessions_n,
    }


def network_comparison(since: int, until: int) -> list[dict]:
    """Per-network summary in window: uptime, jitter, outages, longest."""
    with conn() as c:
        labels_rows = c.execute(
            """
            SELECT DISTINCT COALESCE(s.city || ' — ' || s.isp, s.label) AS label
            FROM sessions s JOIN probes p ON p.session_id = s.id
            WHERE s.start_ts <= ? AND (s.end_ts IS NULL OR s.end_ts >= ?)
              AND p.ts >= ? AND p.ts <= ?
            """,
            (until, since, since, until),
        ).fetchall()
    out = []
    for lr in labels_rows:
        label = lr["label"]
        ids = session_ids_for_labels([label], since, until)
        if not ids:
            continue
        per_layer = per_layer_per_second(since, until, ids)
        layer_outs = {l: compute_layer_outages(per_layer.get(l, [])) for l in LAYERS}
        quality = quality_per_layer(since, until, ids)
        isp_down = isp_attributed_downtime_sec(per_layer)
        # Lan-up bound: total seconds we sampled with LAN known up (covered time).
        covered = len(per_layer.get("lan", [])) * INTERVAL_SEC
        out.append({
            "label": label,
            "covered_sec": covered,
            "isp_downtime_sec": isp_down,
            "isp_uptime_pct": round((1 - isp_down / covered) * 100, 3) if covered else None,
            "isp_outage_count": len(layer_outs.get("isp", [])) + len(layer_outs.get("wan", [])),
            "longest_isp_outage_sec": max(
                (o["duration_sec"] for layer in ("isp", "wan")
                 for o in layer_outs.get(layer, [])),
                default=0,
            ),
            "wan_p50_ms": quality.get("wan", {}).get("p50_ms") if quality.get("wan") else None,
            "wan_p95_ms": quality.get("wan", {}).get("p95_ms") if quality.get("wan") else None,
            "wan_jitter_ms": quality.get("wan", {}).get("jitter_ms") if quality.get("wan") else None,
            "wan_loss_pct": quality.get("wan", {}).get("loss_pct") if quality.get("wan") else None,
        })
    out.sort(key=lambda x: x["covered_sec"], reverse=True)
    return out


@app.route("/api/status")
def api_status():
    hours = int(request.args.get("hours", 24))
    selected_labels = request.args.getlist("network")  # repeated query param

    # Optional explicit range (from chart zoom). When set, overrides hours.
    from_ts = request.args.get("from_ts")
    to_ts = request.args.get("to_ts")
    zoomed = bool(from_ts and to_ts)
    if zoomed:
        since = int(from_ts)
        until = int(to_ts)
    else:
        until = int(time.time())
        since = until - hours * 3600

    current = get_current_session()
    current_label = network_label(current) if current else None

    # --- CURRENT NETWORK zone -----------------------------------------------
    current_session_ids = session_ids_for_same_network(current, since, until) if current else []
    if not current_session_ids and current:
        current_session_ids = [current["id"]]
    cur_per_layer = per_layer_per_second(since, until, current_session_ids)
    cur_layer_outages = {l: compute_layer_outages(cur_per_layer.get(l, [])) for l in LAYERS}
    current_stats = compute_stats(cur_per_layer, cur_layer_outages)
    current_status = live_status_per_layer(current_session_ids)
    current_quality = quality_per_layer(since, until, current_session_ids)
    current_sparkline = sparkline_rtt(current_session_ids, 300)
    current_baseline = baseline_rtt(current_session_ids, 7)
    current_evidence = {
        "isp_downtime_sec": isp_attributed_downtime_sec(cur_per_layer),
        **mtbf_and_streak(cur_per_layer, cur_layer_outages, until),
    }
    # Text-based pattern insights driven by the same window as the rest of the
    # section (so it respects the Window dropdown).
    current_patterns = pattern_insights(since, until, current_session_ids)

    # --- HISTORICAL ANALYSIS zone -------------------------------------------
    available_networks = get_available_networks(since, until)
    if not selected_labels:
        # Default selection = current network only.
        selected_labels = [current_label] if current_label else []
    selected_session_ids = session_ids_for_labels(selected_labels, since, until)

    sel_per_layer = per_layer_per_second(since, until, selected_session_ids)
    n_target_buckets = 720
    bucket_sec = max(INTERVAL_SEC, (until - since) // n_target_buckets)
    times = bucket_times(since, until, bucket_sec)
    chart = {l: bucket_down_ratio(sel_per_layer.get(l, []), since, until, bucket_sec)
             for l in LAYERS}
    wan_rtt_by_label = wan_rtt_bucketed_by_label(
        selected_session_ids, since, until, bucket_sec,
    )
    sel_layer_outages = {l: compute_layer_outages(sel_per_layer.get(l, [])) for l in LAYERS}
    selected_outages = attribute_outages(sel_layer_outages, since, until,
                                         selected_session_ids)
    selected_stats = compute_stats(sel_per_layer, sel_layer_outages)

    comparison = network_comparison(since, until)

    return jsonify({
        # Current network
        "current_session": current,
        "current_network_label": current_label,
        "current_stats_per_layer": current_stats,
        "current_status_per_layer": current_status,
        "current_quality_per_layer": current_quality,
        "current_sparkline": current_sparkline,
        "current_baseline": current_baseline,
        "current_evidence": current_evidence,
        "current_patterns": current_patterns,

        # Comparison + health
        "network_comparison": comparison,
        "monitor_health": monitor_health(),

        # Historical analysis
        "available_networks": available_networks,
        "selected_networks": selected_labels,
        "selected_stats_per_layer": selected_stats,

        "window_hours": hours,
        "zoomed": zoomed,
        "since": since,
        "until": until,
        "bucket_sec": bucket_sec,
        "bucket_times": times,
        "chart": chart,
        "wan_rtt_by_label": wan_rtt_by_label,
        "outages": selected_outages,

        "layer_titles": LAYER_TITLES,
    })


INDEX_HTML = r"""
<!doctype html>
<html><head>
<meta charset="utf-8">
<title>NetMonitor — ISP outage tracker</title>
<script src="/static/plotly.min.js"></script>
<style>
 :root{
   /* Surfaces — cool, neutral, single elevation */
   --bg:#FAFAF9;
   --surface:#FFFFFF;
   --surface-raised:#FFFFFF;
   --surface-hover:#F4F4F5;

   /* Borders — light gray scale */
   --border:#E5E5E5;
   --border-strong:#D4D4D8;

   /* Text — zinc scale */
   --text:#18181B;
   --text-muted:#52525B;
   --text-faint:#71717A;

   /* Single accent: indigo */
   --accent:#2563EB;
   --accent-700:#1D4ED8;
   --accent-soft:#DBEAFE;

   /* Semantic — restrained, used only on actual status data */
   --ok:#16A34A;       --ok-soft:#DCFCE7;
   --warn:#D97706;     --warn-soft:#FFEDD5;
   --bad:#DC2626;      --bad-soft:#FEE2E2;

   /* Legacy aliases (kept so existing class names keep working) */
   --paper:var(--bg); --cream:var(--surface); --card:var(--surface);
   --rule:var(--border); --rule-strong:var(--border-strong);
   --onyx:var(--text); --info:var(--accent);
   --tangerine:var(--accent); --tangerine-700:var(--accent-700);
   --tangerine-100:var(--accent-soft);
   --sienna:var(--text-faint); --sienna-700:var(--text-muted);
   --sienna-100:#F4F4F5;
   --tuscan:var(--accent); --tuscan-100:var(--accent-soft);
   --midnight:var(--accent);
 }
 *{box-sizing:border-box}
 html,body{margin:0; padding:0; background:var(--bg); color:var(--text)}
 body{
   font-family:-apple-system,BlinkMacSystemFont,"Inter","Segoe UI",sans-serif;
   font-size:14px; line-height:1.5; -webkit-font-smoothing:antialiased;
 }
 .page{padding:16px 32px 32px}

 h1{margin:0; font-size:24px; font-weight:700; letter-spacing:-.3px}
 h2{margin:0 0 14px; font-size:12px; text-transform:uppercase;
    letter-spacing:1.3px; font-weight:700}
 h3{margin:18px 0 10px; font-size:11px; color:var(--text-faint);
    text-transform:uppercase; letter-spacing:.9px; font-weight:600}

 /* Top bar */
 .topbar{
   padding:22px 32px 14px; display:flex; justify-content:space-between;
   align-items:baseline; gap:16px;
 }
 .topbar .sub{color:var(--text-faint); font-size:14px}
 #lastUpdate{color:var(--text-faint); font-size:12px; white-space:nowrap}

 /* Loading indicator — only visible while a user-triggered filter change
    (window or networks) is fetching+rendering. The 10s auto-refresh does
    NOT trigger this. Sits inline with the Historical controls. */
 #loadingIndicator{
   display:none; align-items:center; gap:6px;
   font-size:12px; color:var(--text-faint);
   padding:3px 10px; border:1px solid var(--border);
   border-radius:999px; background:var(--surface);
   white-space:nowrap; margin-left:auto;
 }
 body.is-loading #loadingIndicator{display:inline-flex}
 #loadingIndicator .spinner{
   width:10px; height:10px; border-radius:50%;
   border:1.5px solid var(--border);
   border-top-color:var(--accent-700);
   animation:nm-spin .7s linear infinite;
 }
 @keyframes nm-spin{to{transform:rotate(360deg)}}

 /* Sticky connection details */
 .sticky-details{
   position:sticky; top:0; z-index:100;
   background:var(--bg);
   border-top:1px solid var(--border);
   border-bottom:1px solid var(--border);
   padding:12px 32px;
   backdrop-filter:saturate(120%) blur(4px);
   -webkit-backdrop-filter:saturate(120%) blur(4px);
 }
 .session-info{display:flex; flex-wrap:wrap; gap:14px 28px; align-items:center}
 .session-info .field{display:flex; flex-direction:column}
 .session-info .k{font-size:10px; color:var(--text-faint);
                  text-transform:uppercase; letter-spacing:.7px; font-weight:600}
 .session-info .v{font-size:13px; color:var(--text); font-weight:500; margin-top:2px}
 .session-info .v.label{color:var(--accent-700); font-weight:700}

 /* Section cards — plain white, subtle border. Section identity comes from
    the heading's small colored dot, not background tints. */
 section.zone{
   background:var(--surface);
   border:1px solid var(--border);
   border-radius:12px;
   padding:22px 26px;
   margin-bottom:16px;
 }
 section.zone .desc{color:var(--text-muted); font-size:13px;
                    margin-top:-6px; margin-bottom:14px}
 section.zone h2{
   display:inline-flex; align-items:center; gap:10px;
   color:var(--text); margin-bottom:18px;
 }
 section.zone h2::before{
   content:""; display:inline-block;
   width:8px; height:8px; border-radius:50%;
   background:var(--text-faint);
 }
 section.zone.evidence h2::before{background:var(--bad)}
 section.zone.current  h2::before{background:var(--accent)}
 section.zone.history  h2::before{background:var(--text-muted)}

 /* === Condensed ISP service report === */
 .report-strip{
   display:grid;
   grid-template-columns: minmax(220px, 1fr) minmax(360px, 2fr) minmax(220px, 1.2fr);
   gap:14px;
 }
 .report-hero{
   background:var(--bad-soft);
   border:1px solid #FCA5A5;
   border-radius:10px;
   padding:14px 18px;
   display:flex; flex-direction:column; justify-content:center;
 }
 .report-hero .num{
   font-size:34px; font-weight:700; line-height:1;
   font-variant-numeric:tabular-nums; color:var(--bad); letter-spacing:-.5px;
 }
 .report-hero .cap{
   font-size:10px; text-transform:uppercase; letter-spacing:.7px;
   font-weight:600; color:var(--bad); margin-top:6px;
 }
 .report-stats{
   display:grid; grid-template-columns:repeat(4, 1fr); gap:8px;
 }
 .report-stats .rs{
   background:var(--surface-raised);
   border:1px solid var(--border); border-radius:8px;
   padding:10px 12px;
 }
 .report-stats .rs .v{
   font-size:17px; font-weight:600; font-variant-numeric:tabular-nums; line-height:1;
 }
 .report-stats .rs .l{
   font-size:9.5px; color:var(--text-faint); text-transform:uppercase;
   letter-spacing:.6px; margin-top:5px; font-weight:600;
 }
 .report-insight{
   background:var(--accent-soft);
   border:1px solid #93C5FD;
   border-radius:10px;
   padding:12px 14px;
   display:flex; flex-direction:column; justify-content:center;
 }
 .report-insight .ri-label{
   font-size:9.5px; color:var(--accent-700); text-transform:uppercase;
   letter-spacing:.7px; font-weight:700;
 }
 .report-insight .ri-text{
   font-size:13px; margin-top:6px; color:var(--text); line-height:1.4;
 }
 .report-insight .ri-meta{
   font-size:10px; color:var(--text-faint); margin-top:6px;
 }

 /* === Layer cards === */
 .layer-row{display:flex; gap:14px; flex-wrap:wrap}
 .layer-card{
   flex:1; min-width:260px;
   background:var(--surface-raised);
   border:1px solid var(--border);
   border-radius:12px;
   padding:18px 20px 16px;
   transition:border-color .2s, box-shadow .2s;
 }
 .layer-card.lc-up      {border-color:var(--ok);   box-shadow:0 0 0 1px var(--ok-soft) inset}
 .layer-card.lc-down    {border-color:var(--bad);  box-shadow:0 0 0 1px var(--bad-soft) inset}
 .layer-card.lc-degraded{border-color:var(--warn); box-shadow:0 0 0 1px var(--warn-soft) inset}
 .layer-card.lc-stale   {border-color:var(--warn); box-shadow:0 0 0 1px var(--warn-soft) inset}

 .layer-card .head{display:flex; justify-content:space-between; align-items:center;
                   margin-bottom:14px; gap:10px}
 .layer-card .name{font-size:11px; color:var(--text-muted);
                   text-transform:uppercase; letter-spacing:.9px; font-weight:700}

 .pill{display:inline-flex; align-items:center; padding:4px 11px;
       border-radius:12px; font-size:11px; font-weight:700; letter-spacing:.6px}
 .pill::before{content:""; width:6px; height:6px; border-radius:50%;
               background:currentColor; margin-right:6px}
 .pill-up{background:var(--ok-soft); color:var(--ok)}
 .pill-down{background:var(--bad); color:#fff}
 .pill-degraded{background:var(--warn); color:#fff}
 .pill-stale{background:var(--warn-soft); color:var(--warn)}
 .pill-unknown{background:var(--sienna-100); color:var(--sienna)}

 .uptime-hero{display:flex; align-items:baseline; gap:6px; line-height:1; margin-bottom:2px}
 .uptime-num{
   font-size:40px; font-weight:700; letter-spacing:-1px;
   font-variant-numeric:tabular-nums;
 }
 .uptime-num.up-excellent{color:var(--ok)}
 .uptime-num.up-good     {color:#4FA169}
 .uptime-num.up-warn     {color:var(--warn)}
 .uptime-num.up-bad      {color:var(--bad)}
 .uptime-num.up-unknown  {color:var(--text-faint)}
 .uptime-pct{font-size:18px; font-weight:600; color:var(--text-faint)}
 .uptime-cap{font-size:10px; color:var(--text-faint); text-transform:uppercase;
             letter-spacing:.8px; margin-bottom:14px; font-weight:700}

 .baseline-tag{
   display:inline-flex; align-items:center; padding:2px 8px; border-radius:8px;
   font-size:10px; font-weight:700; letter-spacing:.5px; margin-left:10px;
   vertical-align:middle;
 }
 .baseline-tag.bl-normal  {background:var(--ok-soft); color:var(--ok)}
 .baseline-tag.bl-elevated{background:var(--warn-soft); color:var(--warn)}
 .baseline-tag.bl-bad     {background:var(--bad-soft); color:var(--bad)}

 .sparkline-wrap{margin-top:8px; height:32px}
 .sparkline-wrap svg{width:100%; height:100%; overflow:visible}

 .quality{display:flex; gap:14px; flex-wrap:wrap; margin-top:10px;
          font-size:11px; color:var(--text-faint)}
 .quality span{display:inline-flex; gap:4px; align-items:baseline}
 .quality span b{color:var(--text); font-weight:700; font-variant-numeric:tabular-nums}
 .quality span.bad b{color:var(--bad)}
 .quality span.warn b{color:var(--warn)}

 .substats{display:flex; gap:18px; padding-top:12px; margin-top:10px;
           border-top:1px solid var(--border)}
 .substat{flex:1}
 .substat .n{font-size:18px; font-weight:600; font-variant-numeric:tabular-nums; line-height:1}
 .substat .n.attn{color:var(--warn)}
 .substat .l{font-size:10px; color:var(--text-faint); text-transform:uppercase;
             letter-spacing:.6px; margin-top:4px; font-weight:600}

 /* === Controls === */
 .controls{display:flex; gap:14px; align-items:center; flex-wrap:wrap;
           margin-bottom:14px}
 .controls label.ctl{display:flex; align-items:center; gap:8px;
                     color:var(--text-faint); font-size:11px;
                     text-transform:uppercase; letter-spacing:.7px; font-weight:600}
 select{background:var(--surface-raised); color:var(--text);
        border:1px solid var(--border); border-radius:8px;
        padding:6px 10px; font:inherit; cursor:pointer}
 select:hover{border-color:var(--border-strong)}

 details.netpicker{position:relative}
 details.netpicker summary{
   list-style:none; cursor:pointer;
   background:var(--surface-raised); color:var(--text);
   border:1px solid var(--border); border-radius:8px;
   padding:6px 12px; font-size:13px; min-width:240px;
   display:inline-flex; align-items:center; justify-content:space-between; gap:8px;
 }
 details.netpicker summary::-webkit-details-marker{display:none}
 details.netpicker summary::after{content:"▾"; color:var(--text-faint); font-size:10px}
 details.netpicker[open] summary::after{content:"▴"}
 details.netpicker .menu{
   position:absolute; top:calc(100% + 4px); left:0; z-index:50;
   background:var(--surface-raised); border:1px solid var(--border); border-radius:8px;
   padding:6px 4px; min-width:300px; max-height:340px; overflow-y:auto;
   box-shadow:0 8px 20px rgba(22,22,22,0.10);
 }
 details.netpicker .menu .row{
   display:flex; align-items:center; gap:8px; padding:6px 10px;
   border-radius:6px; cursor:pointer; font-size:13px;
 }
 details.netpicker .menu .row:hover{background:var(--tangerine-100)}
 details.netpicker .menu .row input{margin:0; cursor:pointer; accent-color:var(--accent)}
 details.netpicker .menu .row .count{margin-left:auto; color:var(--text-faint); font-size:11px}
 details.netpicker .menu-actions{
   display:flex; gap:6px; padding:4px 6px 6px;
   border-bottom:1px solid var(--border); margin-bottom:4px;
 }
 details.netpicker .menu-actions button{
   background:transparent; color:var(--accent-700); border:none; cursor:pointer;
   font-size:10px; padding:2px 6px; text-transform:uppercase;
   letter-spacing:.5px; font-weight:700;
 }
 details.netpicker .menu-actions button:hover{text-decoration:underline}

 #zoomBadge{
   display:none; color:var(--accent-700);
   font-size:12px; padding:5px 10px;
   border:1px solid var(--accent); border-radius:8px;
   background:var(--tangerine-100); font-weight:600;
 }
 #zoomBadge button{
   margin-left:8px; background:none; border:none; color:var(--accent-700);
   cursor:pointer; font-size:12px; text-decoration:underline; font-weight:700;
 }

 /* === Charts === */
 .panel{
   background:var(--surface-raised);
   border:1px solid var(--border);
   border-radius:10px; padding:10px;
   width:100%;
 }
 #heatmap, #rttChart{width:100%; height:230px}

 /* === Tables === */
 table{
   width:100%; border-collapse:collapse;
   background:var(--surface-raised);
   border:1px solid var(--border);
   border-radius:10px; overflow:hidden;
 }
 th,td{
   padding:10px 14px; text-align:left;
   border-bottom:1px solid var(--border); font-size:13.5px;
 }
 th{
   background:var(--surface-hover); color:var(--text-muted);
   font-weight:600; text-transform:uppercase; letter-spacing:.6px; font-size:11px;
 }
 tr:last-child td{border-bottom:none}
 tbody tr:hover{background:var(--surface-hover)}
 #outageTable thead th:hover{ color:var(--accent); background:var(--surface-hover) }
 .attr-cell{display:flex; flex-direction:column; gap:3px}
 .attr-cell .verdict{font-weight:700; font-size:12px}
 .attr-cell .summary{color:var(--text); font-size:12.5px; line-height:1.4}
 .attr-cell .evidence{color:var(--text-faint); font-size:11px;
                      font-variant-numeric:tabular-nums; line-height:1.35}
 .attr-lan .verdict{color:var(--warn)}
 .attr-isp .verdict{color:var(--bad)}
 .attr-wan .verdict{color:var(--info)}
 .attr-web .verdict{color:var(--sienna)}

 /* === Inline help (?) === */
 .help{
   display:inline-flex; align-items:center; justify-content:center;
   width:15px; height:15px; margin-left:8px;
   border:1px solid var(--border); border-radius:50%;
   color:var(--text-faint); font-size:10px; font-weight:700;
   cursor:help; position:relative; vertical-align:middle;
   text-transform:none; letter-spacing:0;
   user-select:none;
 }
 .help:hover{ color:var(--accent); border-color:var(--accent); }
 .help::after{
   content:attr(data-help);
   position:absolute; bottom:calc(100% + 8px); left:50%;
   transform:translateX(-50%);
   background:var(--surface-raised); color:var(--text);
   border:1px solid var(--border); border-radius:6px;
   padding:9px 11px; font-size:12px; font-weight:400;
   line-height:1.45; width:300px; white-space:normal;
   text-transform:none; letter-spacing:0;
   opacity:0; pointer-events:none; transition:opacity .15s;
   box-shadow:0 6px 16px rgba(22,22,22,.10);
   z-index:100;
 }
 .help:hover::after{ opacity:1; }
 .comparison-table td.num{font-variant-numeric:tabular-nums; text-align:right}
 .comparison-table tr.current-row{background:var(--tangerine-100)}
 .comparison-table tr.current-row td:first-child::before{
   content:"● "; color:var(--accent)
 }

 /* === Monitor health footer === */
 footer.health{
   margin-top:18px; padding:14px 20px;
   background:var(--surface-raised);
   border:1px solid var(--border); border-radius:10px;
   display:flex; gap:24px; flex-wrap:wrap;
   font-size:11px; color:var(--text-faint);
   text-transform:uppercase; letter-spacing:.6px;
 }
 footer.health .item b{
   display:block; font-size:14px; color:var(--text); font-weight:700;
   font-variant-numeric:tabular-nums;
   text-transform:none; letter-spacing:0; margin-bottom:2px;
 }
 footer.health .item b.warn{color:var(--warn)}
 footer.health .item b.bad{color:var(--bad)}
</style>
</head><body>

<div class="topbar">
  <div>
    <h1>NetMonitor <span style="font-size:14px; color:var(--text-faint); font-weight:400">— ISP outage tracker</span></h1>
    <div class="sub" id="topSub">…</div>
  </div>
  <div id="lastUpdate"></div>
</div>

<!-- Sticky connection details -->
<div class="sticky-details">
  <div class="session-info" id="sessionInfo"></div>
</div>

<div class="page">

  <!-- ISP service report (condensed, at top) -->
  <section class="zone evidence">
    <h2>ISP service report — this network<span class="help" data-help="Summarizes ISP-attributable downtime in the selected window. Counts seconds where your LAN was up but ISP edge, WAN, or Web layers were down — those are faults on the provider's side, not yours. MTBF = mean time between ISP-attributed failures. Uptime streak = time since the last such failure.">?</span></h2>
    <div class="desc">Outages where the ISP failed to deliver — your router was up but the ISP edge or internet was unreachable. Screenshot for support calls.</div>

    <div class="report-strip">
      <div class="report-hero">
        <div class="num" id="evDowntime">—</div>
        <div class="cap" id="evDowntimeCap">ISP downtime in window</div>
      </div>
      <div class="report-stats">
        <div class="rs"><div class="v" id="evOutages">—</div><div class="l">Outages</div></div>
        <div class="rs"><div class="v" id="evMtbf">—</div><div class="l">Avg between</div></div>
        <div class="rs"><div class="v" id="evStreak">—</div><div class="l">Uptime streak</div></div>
        <div class="rs"><div class="v" id="evLastOutage">—</div><div class="l">Last outage</div></div>
      </div>
      <div class="report-insight">
        <div class="ri-label">Pattern</div>
        <div class="ri-text" id="insightText">—</div>
        <div class="ri-meta" id="insightMeta"></div>
      </div>
    </div>
  </section>

  <!-- Current network live status -->
  <section class="zone current">
    <h2>Current network — live<span class="help" data-help="Live status of each probe layer right now, with uptime and quality stats over the selected window. UP = all targets responding. DEGRADED = some targets failing (partial packet loss / brownout). DOWN = every target failing. STALE = no recent probe in the last 15s. Layers: LAN (your router) → ISP edge → WAN (Cloudflare/Google ICMP) → Web (DNS + HTTPS).">?</span></h2>
    <div class="desc" id="liveDesc">Live layer status, uptime over selected window.</div>
    <div class="layer-row" id="currentLayerRow"></div>
  </section>

  <!-- Historical analysis -->
  <section class="zone history">
    <h2>Historical analysis</h2>

    <div class="controls">
      <label class="ctl">Window
        <select id="hours" onchange="onHoursChange()">
          <option value="1">Last 1 hour</option>
          <option value="3">Last 3 hours</option>
          <option value="6">Last 6 hours</option>
          <option value="24" selected>Last 24 hours</option>
          <option value="72">Last 3 days</option>
          <option value="168">Last 7 days</option>
          <option value="336">Last 2 weeks</option>
          <option value="1344">Last 8 weeks</option>
        </select>
      </label>
      <label class="ctl">Networks
        <details class="netpicker" id="netpicker">
          <summary><span id="netpickerLabel">…</span></summary>
          <div class="menu" id="netpickerMenu"></div>
        </details>
      </label>
      <span id="zoomBadge">
        <span id="zoomBadgeText"></span>
        <button onclick="resetZoom()">reset</button>
      </span>
      <span id="loadingIndicator"><span class="spinner"></span>Updating…</span>
    </div>

    <h3>Outage timeline <span style="text-transform:none; letter-spacing:0; font-size:11px; color:var(--text-faint)">— green = healthy, amber = partial, red = down</span><span class="help" data-help="Color = fraction of probes that failed in each time bucket, per layer. Green = clean, amber = some loss, red = heavy loss. Unlike a pure up/down view, this surfaces partial loss — e.g. one of two WAN targets failing — so brownouts no longer hide. Hover any cell to see the loss %.">?</span></h3>
    <div class="panel"><div id="heatmap"></div></div>

    <h3>WAN latency <span style="text-transform:none; letter-spacing:0; font-size:11px; color:var(--text-faint)">— one line per network</span><span class="help" data-help="Median round-trip time to WAN ICMP targets (Cloudflare, Google), bucketed and split by network. A rising trend or sudden jitter often precedes a brownout. Gaps in the line mean the layer was completely unreachable in that bucket.">?</span></h3>
    <div class="panel"><div id="rttChart"></div></div>

    <h3>Outage log<span class="help" data-help="Every event detected in the selected window. DOWN (red pill) = every target in that layer failed simultaneously — a hard outage. DEGRADED (amber pill) = some targets failed but at least one still worked — partial loss / brownout. Click any column header to sort; click again to reverse.">?</span></h3>
    <table id="outageTable">
      <thead><tr><th>Start</th><th>End</th><th>Duration</th><th>Layer</th><th>Likely cause</th></tr></thead>
      <tbody></tbody>
    </table>

    <div id="comparisonBlock" style="display:none">
      <h3 style="margin-top:22px">Network comparison<span class="help" data-help="Side-by-side stats for every network you've used in the selected window — useful for ranking ISPs or locations by reliability. ISP uptime % counts only ISP-attributable downtime (LAN-up but upstream-down), so router issues don't penalize the ISP.">?</span></h3>
      <table class="comparison-table">
        <thead>
          <tr>
            <th>Network</th><th>ISP uptime</th><th>Outages</th><th>Longest</th>
            <th>WAN p50</th><th>WAN p95</th><th>Jitter</th><th>Loss</th>
          </tr>
        </thead>
        <tbody id="comparisonBody"></tbody>
      </table>
    </div>
  </section>

  <footer class="health" id="monitorHealth"></footer>

</div>

<script>
const LAYERS_TOP_DOWN = ["web","wan","isp","lan"];
const LAYER_DISPLAY = {lan:"LAN", isp:"ISP edge", wan:"WAN", web:"Web"};

// Light-theme palette for chart text and gridlines.
const CHART_FONT = "#18181B";
const CHART_GRID = "#E5E5E5";

// Distinct trace colors for multi-network WAN latency chart — Tailwind-style hues.
const ISP_PALETTE = ["#2563EB","#DC2626","#16A34A","#9333EA","#D97706","#0891B2","#DB2777","#4F46E5"];
function colorFor(label){
  let h = 0;
  for (let i = 0; i < label.length; i++) h = ((h<<5) - h + label.charCodeAt(i)) | 0;
  return ISP_PALETTE[Math.abs(h) % ISP_PALETTE.length];
}

function fmtDur(s){
  if (s == null) return "—";
  if (s < 60) return s+"s";
  const m=Math.floor(s/60), sec=s%60;
  if (m < 60) return m+"m "+sec+"s";
  const h=Math.floor(m/60), mm=m%60;
  return h+"h "+mm+"m";
}
function fmtTs(ts){ return new Date(ts*1000).toLocaleString(); }
function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }[c]));
}
function fmtTime(ts){ return new Date(ts*1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}); }

function uptimeTone(pct){
  if (pct == null) return "up-unknown";
  if (pct >= 99.9) return "up-excellent";
  if (pct >= 99.0) return "up-good";
  if (pct >= 95.0) return "up-warn";
  return "up-bad";
}
function baselineRatio(current, baseline){
  if (current == null || baseline == null || baseline === 0) return null;
  return current / baseline;
}
function baselineClass(ratio){
  if (ratio == null) return null;
  if (ratio <= 1.5) return "bl-normal";
  if (ratio <= 3.0) return "bl-elevated";
  return "bl-bad";
}

function renderSparkline(points, width, height, color){
  if (!points || !points.length) return `<svg width="${width}" height="${height}"></svg>`;
  const vals = points.map(p => p.rtt).filter(v => v != null);
  if (!vals.length) return `<svg width="${width}" height="${height}"></svg>`;
  const min = Math.min(...vals), max = Math.max(...vals), range = (max - min) || 1;
  const stepX = points.length > 1 ? width / (points.length - 1) : 0;
  const pts = points.map((p, i) => {
    if (p.rtt == null) return null;
    const x = i * stepX;
    const y = height - ((p.rtt - min) / range) * (height - 4) - 2;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const segs = [];
  let cur = [];
  pts.forEach(pt => {
    if (pt == null){ if (cur.length) segs.push(cur); cur = []; }
    else cur.push(pt);
  });
  if (cur.length) segs.push(cur);
  const lines = segs.map(s =>
    `<polyline points="${s.join(' ')}" fill="none" stroke="${color}"
       stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>`
  ).join('');
  return `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">${lines}</svg>`;
}

function getSelectedNetworks(){
  return Array.from(document.querySelectorAll('#netpickerMenu input[type=checkbox]:checked'))
    .map(el => el.value);
}

function buildNetpicker(d){
  const menu = document.getElementById("netpickerMenu");
  const previouslySelected = new Set(d.selected_networks);
  const networks = d.available_networks || [];
  let html = `
    <div class="menu-actions">
      <button type="button" onclick="netpickerAll(true)">Select all</button>
      <button type="button" onclick="netpickerAll(false)">Clear</button>
      <button type="button" onclick="netpickerCurrentOnly()">Current only</button>
    </div>`;
  if (!networks.length){
    html += `<div class="row" style="color:var(--text-faint)">No networks with data in this window</div>`;
  } else {
    networks.forEach(n => {
      const checked = previouslySelected.has(n.label) ? "checked" : "";
      const isCur = (n.label === d.current_network_label) ? " (current)" : "";
      html += `
        <label class="row">
          <input type="checkbox" value="${n.label}" ${checked} onchange="onNetSelectionChange()">
          <span>${n.label}${isCur}</span>
          <span class="count">${n.samples} probes</span>
        </label>`;
    });
  }
  menu.innerHTML = html;
  updateNetpickerSummary(d.current_network_label);
}
function updateNetpickerSummary(currentLabel){
  const selected = getSelectedNetworks();
  const total = document.querySelectorAll('#netpickerMenu input[type=checkbox]').length;
  const s = document.getElementById("netpickerLabel");
  if (selected.length === 0) s.textContent = "(none)";
  else if (selected.length === total && total > 1) s.textContent = `All networks (${total})`;
  else if (selected.length === 1) s.textContent = selected[0];
  else s.textContent = `${selected.length} networks`;
}
function netpickerAll(check){
  document.querySelectorAll('#netpickerMenu input[type=checkbox]').forEach(el => el.checked = check);
  onNetSelectionChange();
}
function netpickerCurrentOnly(){
  const cur = window.__currentNetworkLabel;
  document.querySelectorAll('#netpickerMenu input[type=checkbox]').forEach(el => {
    el.checked = (el.value === cur);
  });
  onNetSelectionChange();
}
function onNetSelectionChange(){
  updateNetpickerSummary(window.__currentNetworkLabel);
  load({userTriggered: true});
}

function renderTop(d){
  const sub = d.current_session
    ? `Connected to <strong>${d.current_network_label}</strong>`
    : "No active session";
  document.getElementById("topSub").innerHTML = sub;
}

function renderSessionInfo(d){
  const c = document.getElementById("sessionInfo");
  const s = d.current_session;
  if (!s){ c.innerHTML = "<div class=field>No session data yet</div>"; return; }
  const items = [
    ["Network",    `<span class="v label">${d.current_network_label || "—"}</span>`],
    ["City",       [s.city, s.region, s.country].filter(Boolean).join(", ") || "—"],
    ["Public IP",  s.public_ip || "—"],
    ["ASN",        s.asn || "—"],
    ["Gateway",    s.gateway_ip || "—"],
    ["ISP edge",   s.isp_edge_ip || "—"],
    ["SSID",       s.ssid || "(wired)"],
    ["Started",    fmtTime(s.start_ts)],
  ];
  c.innerHTML = items.map(([k,v]) =>
    `<div class="field"><div class="k">${k}</div>` +
    (v.startsWith('<span') ? v : `<div class="v">${v}</div>`) +
    `</div>`).join("");
}

function renderCurrentLayerRow(d){
  const container = document.getElementById("currentLayerRow");
  container.innerHTML = "";
  const sparkColor = {lan:"#16A34A", isp:"#D97706", wan:"#2563EB", web:"#9333EA"};

  ["lan","isp","wan","web"].forEach(layer => {
    const st = d.current_stats_per_layer[layer] || {};
    const status = d.current_status_per_layer[layer] || "unknown";
    const q = (d.current_quality_per_layer || {})[layer] || {};
    const spark = (d.current_sparkline || {})[layer] || [];
    const baseline = (d.current_baseline || {})[layer];

    const pct = st.uptime_pct;
    const num = pct == null ? "—" : pct.toFixed(2);
    const tone = uptimeTone(pct);
    const outages = st.outages ?? 0;
    const longest = fmtDur(st.longest_sec || 0);

    const recentVals = spark.map(p => p.rtt).filter(v => v != null).sort((a,b)=>a-b);
    const recentMedian = recentVals.length ? recentVals[Math.floor(recentVals.length/2)] : null;
    const ratio = baselineRatio(recentMedian, baseline);
    const ratioCls = baselineClass(ratio);
    let baselineHtml = "";
    if (ratio != null){
      const label = ratio <= 1.5 ? "normal" : `${ratio.toFixed(1)}× baseline`;
      baselineHtml = `<span class="baseline-tag ${ratioCls}" title="recent median ${recentMedian.toFixed(1)}ms / baseline ${baseline.toFixed(1)}ms">${label}</span>`;
    }

    const fmt = (v, unit) => (v == null ? "—" : `${v.toFixed(1)}${unit}`);
    const lossPct = q.loss_pct ?? null;
    const lossCls = lossPct == null ? "" : (lossPct >= 1 ? "bad" : (lossPct >= 0.1 ? "warn" : ""));
    const jitterCls = q.jitter_ms == null ? "" : (q.jitter_ms >= 50 ? "bad" : (q.jitter_ms >= 20 ? "warn" : ""));
    const qualityRow = `
      <div class="quality">
        <span>p50 <b>${fmt(q.p50_ms,"ms")}</b></span>
        <span>p95 <b>${fmt(q.p95_ms,"ms")}</b></span>
        <span>p99 <b>${fmt(q.p99_ms,"ms")}</b></span>
        <span class="${jitterCls}">jitter <b>${fmt(q.jitter_ms,"ms")}</b></span>
        <span class="${lossCls}">loss <b>${lossPct == null ? "—" : lossPct.toFixed(2)+"%"}</b></span>
      </div>`;

    const card = document.createElement("div");
    card.className = `layer-card lc-${status}`;
    card.innerHTML = `
      <div class="head">
        <div class="name">${d.layer_titles[layer]}</div>
        <span class="pill pill-${status}">${status.toUpperCase()}</span>
      </div>
      <div class="uptime-hero">
        <span class="uptime-num ${tone}">${num}</span>
        <span class="uptime-pct">%</span>
        ${baselineHtml}
      </div>
      <div class="uptime-cap">Uptime</div>
      <div class="sparkline-wrap" title="Last 5 min RTT">
        ${renderSparkline(spark, 240, 32, sparkColor[layer])}
      </div>
      ${qualityRow}
      <div class="substats">
        <div class="substat">
          <div class="n ${outages > 0 ? 'attn' : ''}">${outages}</div>
          <div class="l">Outages</div>
        </div>
        <div class="substat">
          <div class="n">${longest}</div>
          <div class="l">Longest</div>
        </div>
      </div>`;
    container.appendChild(card);
  });
}

function renderEvidence(d){
  const ev = d.current_evidence || {};
  document.getElementById("evDowntime").textContent = fmtDur(ev.isp_downtime_sec || 0);
  const span = (d.until - d.since) || 1;
  const pct = ((ev.isp_downtime_sec || 0) / span) * 100;
  document.getElementById("evDowntimeCap").textContent =
    `ISP downtime · ${pct.toFixed(2)}% of window`;
  document.getElementById("evOutages").textContent = ev.isp_outage_count ?? 0;
  document.getElementById("evMtbf").textContent =
    ev.mtbf_sec == null ? "—" : fmtDur(ev.mtbf_sec);
  document.getElementById("evStreak").textContent = fmtDur(ev.streak_sec || 0);
  document.getElementById("evLastOutage").textContent =
    ev.last_outage_at ? fmtTime(ev.last_outage_at) : "(none)";

  const p = d.current_patterns || {insights: ["—"], covered_sec: 0};
  document.getElementById("insightText").textContent = (p.insights && p.insights[0]) || "—";
  const coveredHrs = (p.covered_sec || 0) / 3600;
  document.getElementById("insightMeta").textContent =
    `based on ${coveredHrs.toFixed(1)}h of data on this network`;
}

function fmtRange(sinceTs, untilTs){
  const sameDay = (new Date(sinceTs*1000)).toDateString() ===
                  (new Date(untilTs*1000)).toDateString();
  const t = ts => new Date(ts*1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
  const d = ts => new Date(ts*1000).toLocaleString([], {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"});
  return sameDay ? `${t(sinceTs)}–${t(untilTs)}` : `${d(sinceTs)} – ${d(untilTs)}`;
}
function updateLiveDesc(d){
  const span = (d.until - d.since);
  let rangeLabel;
  if (d.zoomed) rangeLabel = `Zoomed: ${fmtRange(d.since, d.until)}`;
  else if (span <= 3600 * 1.5) rangeLabel = `Last ${Math.round(span/60)}m`;
  else if (span <= 86400 * 1.5) rangeLabel = `Last ${Math.round(span/3600)}h`;
  else rangeLabel = `Last ${Math.round(span/86400)}d`;
  document.getElementById("liveDesc").textContent =
    `${rangeLabel} on this network — not affected by the Networks selector.`;

  const badge = document.getElementById("zoomBadge");
  if (d.zoomed){
    document.getElementById("zoomBadgeText").textContent =
      `Zoomed: ${fmtRange(d.since, d.until)}`;
    badge.style.display = "inline-block";
  } else {
    badge.style.display = "none";
  }
}

function renderHeatmap(d){
  const el = document.getElementById("heatmap");
  if (!el) return;
  const xs = (d.bucket_times || []).map(ts => new Date(ts*1000));
  const z = LAYERS_TOP_DOWN.map(l => (d.chart || {})[l] || []);
  const sinceMs = d.since * 1000, untilMs = d.until * 1000;
  const trace = {
    type:"heatmap", x: xs, y: LAYERS_TOP_DOWN.map(l => LAYER_DISPLAY[l]), z,
    zmin:0, zmax:1, showscale:false,
    colorscale: [[0,"#16A34A"], [0.5,"#FBBF24"], [1,"#DC2626"]],
    hovertemplate: "%{y}<br>%{x}<br>loss: %{z:.0%}<extra></extra>",
    xgap:0, ygap:2,
  };
  Plotly.react(el,[trace],{
    margin:{l:80,r:20,t:10,b:40},
    paper_bgcolor:"transparent", plot_bgcolor:"transparent",
    font:{color:CHART_FONT, size:12},
    xaxis:{gridcolor:CHART_GRID, linecolor:CHART_GRID,
           range:[sinceMs, untilMs], type:"date"},
    yaxis:{automargin:true, linecolor:CHART_GRID},
  },{displayModeBar:false, responsive:true})
  .then(setupZoomSync)
  .catch(err => console.error("heatmap render", err));
}

function renderRtt(d){
  const el = document.getElementById("rttChart");
  if (!el) return;
  const groups = d.wan_rtt_by_label || [];
  const sinceMs = d.since * 1000, untilMs = d.until * 1000;
  const traces = groups.map(g => ({
    x: g.points.map(p => new Date(p.ts*1000)),
    y: g.points.map(p => p.rtt),
    type:"scatter", mode:"lines",
    name: g.label, connectgaps:false,
    line:{ width:1.8, color: colorFor(g.label) },
  }));
  Plotly.react(el, traces, {
    margin:{l:50, r:20, t:30, b:40},
    paper_bgcolor:"transparent", plot_bgcolor:"transparent",
    font:{color:CHART_FONT, size:12},
    xaxis:{gridcolor:CHART_GRID, linecolor:CHART_GRID,
           range:[sinceMs, untilMs], type:"date"},
    yaxis:{title:"ms", gridcolor:CHART_GRID, linecolor:CHART_GRID, rangemode:"tozero"},
    showlegend:true,
    legend:{orientation:"h", y:1.15, bgcolor:"rgba(0,0,0,0)"},
  }, {displayModeBar:false, responsive:true})
  .then(setupZoomSync)
  .catch(err => console.error("rtt render", err));
}

const OUTAGE_SORT_KEYS = ["start", "end", "duration_sec", "layer", "attribution"];
let outageSort = { key: "start", dir: "desc" };

function outageSortValue(o, key){
  if (key === "layer") {
    const i = LAYERS_TOP_DOWN.indexOf(o.layer);
    return i === -1 ? 999 : i;
  }
  if (key === "attribution") {
    // Group by status first (down before degraded), then layer, then start ts.
    const statusRank = (o.status === "down") ? 0 : 1;
    return `${statusRank}|${o.layer}|${o.start}`;
  }
  return o[key];
}

function renderOutages(d){
  const table = document.getElementById("outageTable");
  const tbody = table.querySelector("tbody");
  const ths = table.querySelectorAll("thead th");

  // Wire header click + arrow indicators. Idempotent across re-renders.
  ths.forEach((th, i) => {
    const key = OUTAGE_SORT_KEYS[i];
    if (!key) return;
    const baseLabel = th.dataset.baseLabel || th.textContent;
    th.dataset.baseLabel = baseLabel;
    const arrow = key === outageSort.key ? (outageSort.dir === "asc" ? " ▲" : " ▼") : "";
    th.textContent = baseLabel + arrow;
    th.style.cursor = "pointer";
    th.style.userSelect = "none";
    th.onclick = () => {
      if (outageSort.key === key) {
        outageSort.dir = outageSort.dir === "asc" ? "desc" : "asc";
      } else {
        outageSort.key = key;
        // Sensible defaults: time/duration desc (recent/longest first),
        // categorical asc (top-of-stack first).
        outageSort.dir = (key === "start" || key === "end" || key === "duration_sec") ? "desc" : "asc";
      }
      renderOutages(d);
    };
  });

  tbody.innerHTML = "";
  const outages = d.outages || [];
  if (!outages.length){
    tbody.innerHTML = "<tr><td colspan=5 style='color:var(--text-faint)'>No outages in this window for the selected networks</td></tr>";
    return;
  }
  const sorted = outages.slice().sort((a, b) => {
    const av = outageSortValue(a, outageSort.key);
    const bv = outageSortValue(b, outageSort.key);
    let cmp;
    if (typeof av === "string" || typeof bv === "string") cmp = String(av).localeCompare(String(bv));
    else cmp = (av ?? 0) - (bv ?? 0);
    return outageSort.dir === "asc" ? cmp : -cmp;
  });
  sorted.forEach(o => {
    const tr = document.createElement("tr");
    const status = o.status || "down";
    const badge = `<span class="pill pill-${status}" style="margin-right:8px">${status.toUpperCase()}</span>`;
    const verdict = o.verdict || "";
    const summary = o.summary || o.attribution || "";
    const evidence = o.evidence || "";
    const cell =
      `<div class="attr-cell">` +
        `<div class="verdict">${badge}${escapeHtml(verdict)}</div>` +
        `<div class="summary">${escapeHtml(summary)}</div>` +
        (evidence ? `<div class="evidence">${escapeHtml(evidence)}</div>` : "") +
      `</div>`;
    tr.innerHTML =
      `<td>${fmtTs(o.start)}</td>` +
      `<td>${fmtTs(o.end)}</td>` +
      `<td>${fmtDur(o.duration_sec)}</td>` +
      `<td>${LAYER_DISPLAY[o.layer]}</td>` +
      `<td class="attr-${o.layer}">${cell}</td>`;
    tbody.appendChild(tr);
  });
}

function renderComparison(d){
  const block = document.getElementById("comparisonBlock");
  const rows = d.network_comparison || [];
  if (rows.length < 2){ block.style.display = "none"; return; }
  block.style.display = "block";
  const tbody = document.getElementById("comparisonBody");
  tbody.innerHTML = rows.map(r => {
    const isCurrent = r.label === d.current_network_label;
    const fmt = (v, suf="") => v == null ? "—" : `${v.toFixed(2)}${suf}`;
    return `<tr class="${isCurrent ? 'current-row' : ''}">
      <td>${r.label}</td>
      <td class="num">${fmt(r.isp_uptime_pct, '%')}</td>
      <td class="num">${r.isp_outage_count}</td>
      <td class="num">${fmtDur(r.longest_isp_outage_sec || 0)}</td>
      <td class="num">${fmt(r.wan_p50_ms, 'ms')}</td>
      <td class="num">${fmt(r.wan_p95_ms, 'ms')}</td>
      <td class="num">${fmt(r.wan_jitter_ms, 'ms')}</td>
      <td class="num">${fmt(r.wan_loss_pct, '%')}</td>
    </tr>`;
  }).join("");
}

function renderHealth(d){
  const h = d.monitor_health || {};
  const expected = h.expected_probes_per_60s || 48;
  const lateProbes = h.last_probe_age_sec == null || h.last_probe_age_sec > 30;
  const slowRate = h.probes_last_60s < expected * 0.8;
  const probeClass = lateProbes ? "bad" : (slowRate ? "warn" : "");
  const dbMb = (h.db_bytes / 1048576).toFixed(1);
  const walMb = (h.wal_bytes / 1048576).toFixed(1);
  document.getElementById("monitorHealth").innerHTML = `
    <div class="item"><b class="${probeClass}">${h.probes_last_60s}/${expected}</b>probes last 60s</div>
    <div class="item"><b class="${lateProbes ? 'bad' : ''}">${h.last_probe_age_sec == null ? '—' : h.last_probe_age_sec + 's'}</b>since last probe</div>
    <div class="item"><b>${dbMb} MB</b>db size (wal: ${walMb} MB)</div>
    <div class="item"><b>${h.retention_used_pct}%</b>of ${h.retention_days}d retention used</div>
    <div class="item"><b>${h.sessions_total}</b>sessions total</div>
  `;
}

// Zoom state
const zoomState = { from_ts: null, to_ts: null };
function resetZoom(){
  zoomState.from_ts = null;
  zoomState.to_ts = null;
  load({userTriggered: true});
}
let refetchTimer = null;
function debouncedLoad(){
  clearTimeout(refetchTimer);
  refetchTimer = setTimeout(load, 350);
}
let zoomSyncBound = false;
let zoomSyncing = false;
function setupZoomSync(){
  if (zoomSyncBound) return;
  const ids = ["heatmap","rttChart"];
  if (!ids.every(id => document.getElementById(id) && document.getElementById(id).data)) return;
  ids.forEach(srcId => {
    const others = ids.filter(x => x !== srcId);
    document.getElementById(srcId).on("plotly_relayout", ev => {
      if (zoomSyncing) return;
      const r0 = ev["xaxis.range[0]"], r1 = ev["xaxis.range[1]"];
      const auto = ev["xaxis.autorange"];
      if (r0 == null && r1 == null && !auto) return;
      zoomSyncing = true;
      const update = (r0 != null && r1 != null)
        ? {"xaxis.range":[r0, r1]}
        : {"xaxis.autorange": true};
      Promise.all(others.map(id => Plotly.relayout(id, update)))
        .finally(() => { zoomSyncing = false; });
      if (auto){
        zoomState.from_ts = null; zoomState.to_ts = null;
      } else {
        zoomState.from_ts = Math.floor(new Date(r0).getTime() / 1000);
        zoomState.to_ts   = Math.ceil(new Date(r1).getTime() / 1000);
      }
      debouncedLoad();
    });
  });
  zoomSyncBound = true;
}

// Run a render step in isolation so one failure doesn't stop the others.
function safeRun(name, fn){
  try { fn(); } catch (err) { console.error("render error in " + name + ":", err); }
}

let loadingRevealTimer = null;
let loadingDepth = 0;
function beginLoading(){
  loadingDepth++;
  // Delay reveal so quick auto-refreshes (usually <150ms) don't flash the
  // spinner. Slower fetches (long windows, all networks) will trip it.
  if (loadingDepth === 1 && loadingRevealTimer == null){
    loadingRevealTimer = setTimeout(() => {
      document.body.classList.add("is-loading");
      loadingRevealTimer = null;
    }, 150);
  }
}
function endLoading(){
  loadingDepth = Math.max(0, loadingDepth - 1);
  if (loadingDepth === 0){
    if (loadingRevealTimer != null){
      clearTimeout(loadingRevealTimer);
      loadingRevealTimer = null;
    }
    document.body.classList.remove("is-loading");
  }
}

async function load(opts){
  const userTriggered = !!(opts && opts.userTriggered);
  if (typeof Plotly === "undefined"){
    console.warn("Plotly not loaded yet — charts will render once it arrives.");
  }
  const hours = document.getElementById("hours").value;
  const selected = getSelectedNetworks();
  const params = new URLSearchParams();
  params.set("hours", hours);
  selected.forEach(n => params.append("network", n));
  if (zoomState.from_ts && zoomState.to_ts){
    params.set("from_ts", zoomState.from_ts);
    params.set("to_ts", zoomState.to_ts);
  }

  if (userTriggered) beginLoading();
  let d;
  try {
    const r = await fetch("/api/status?" + params.toString());
    d = await r.json();
  } catch (err) {
    console.error("dashboard fetch failed:", err);
    if (userTriggered) endLoading();
    return;
  }
  window.__currentNetworkLabel = d.current_network_label;

  safeRun("top",         () => renderTop(d));
  safeRun("sessionInfo", () => renderSessionInfo(d));
  safeRun("evidence",    () => renderEvidence(d));
  safeRun("layerRow",    () => renderCurrentLayerRow(d));
  safeRun("liveDesc",    () => updateLiveDesc(d));
  safeRun("netpicker",   () => {
    const sig = (d.available_networks || []).map(n => n.label).sort().join("|");
    if (window.__netpickerSig !== sig){
      buildNetpicker(d);
      window.__netpickerSig = sig;
    } else {
      updateNetpickerSummary(d.current_network_label);
    }
  });
  safeRun("heatmap",    () => renderHeatmap(d));
  safeRun("rtt",        () => renderRtt(d));
  safeRun("outages",    () => renderOutages(d));
  safeRun("comparison", () => renderComparison(d));
  safeRun("health",     () => renderHealth(d));

  {
    const now = new Date();
    const time = now.toLocaleTimeString();
    // Short TZ name (e.g. "IST", "PST") from the browser's locale-aware formatter.
    const tz = new Intl.DateTimeFormat([], { timeZoneName: "short" })
      .formatToParts(now).find(p => p.type === "timeZoneName")?.value || "";
    document.getElementById("lastUpdate").textContent = `Updated at ${time} ${tz}`.trim();
  }
  if (userTriggered) endLoading();
}

document.addEventListener("click", ev => {
  const dp = document.getElementById("netpicker");
  if (dp.open && !dp.contains(ev.target)) dp.open = false;
});

// Restore last-used window from localStorage (default = 24h via HTML `selected`).
(function restoreHours(){
  try {
    const saved = localStorage.getItem("nm.hours");
    if (!saved) return;
    const sel = document.getElementById("hours");
    if ([...sel.options].some(o => o.value === saved)) sel.value = saved;
  } catch (e) {}
})();

function onHoursChange(){
  try { localStorage.setItem("nm.hours", document.getElementById("hours").value); } catch (e) {}
  resetZoom();
}

load();
setInterval(load, 10000);
</script>
</body></html>
"""


@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, debug=False)
