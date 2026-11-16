"""Sidebar Alignment (2026-11-16, Pankaj) — Extra Settings → Sidebar Alignment.

Every <li> of the sidebar in templates/base.html carries
    data-sb="<id>" style="order: {{ sb_order('<id>') }}"
where <id> is the link's href ("/grn", "#appSettingsCollapse") or
"sec:<SECTION>" for a section heading. The sidebar lists are flex columns, so
the CSS order decides the position without moving any Jinja permission
blocks. Default order = position in base.html (x1000). An admin can drag the
items of one section (or of one accordion such as Application Settings) into
a new order; that order is saved in app_settings ("sidebar_alignment", JSON
{group: [ids...]}) and an item then gets <group heading order> + 1 + its new
position, which keeps it inside its own section. Items are not moved between
sections: each section heading only shows when one of its own items is
allowed for the role, so a moved item could end up under a hidden heading.
"""
import json
import os
import re

SETTING_KEY = "sidebar_alignment"
_BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "base.html")
_KEEP_UPPER = {"CRM", "QC", "IQC", "TRC", "GRN", "P&L", "WA", "QA", "PNA"}

_NODES = None          # id -> node dict
_ORDERED = None        # node ids in base.html order
_ALIGN: dict = {}      # group -> [ids] (saved alignment)


def _title(sec):
    return " ".join(w if w.upper() in _KEEP_UPPER else w.capitalize() for w in (sec or "").split())


def _sidebar_region(s):
    a = s.find('<ul class="nav nav-pills flex-column mb-auto px-2"')
    if a < 0:
        return "", 0
    depth = 0
    for m in re.finditer(r"<ul\b|</ul>", s[a:]):
        depth += 1 if m.group() == "<ul" else -1
        if depth == 0:
            return s[a:a + m.end()], a
    return s[a:], a


def node_id_for(li_html_after: str, tag_text: str):
    """The id an <li> gets: its section heading or its first link's href."""
    sec = re.match(r'<li class="nav-section"[^>]*>([^<]+)</li>', tag_text)
    if sec:
        return "sec:" + sec.group(1).strip()
    h = re.search(r'<a\s[^>]*href="([^"]+)"', li_html_after[:500])
    if not h:
        return None
    href = h.group(1)
    return "cosmetic-page" if "{{" in href else href


def parse(force=False):
    """Nodes of the sidebar: id, group (section / accordion), level, label."""
    global _NODES, _ORDERED
    if _NODES is not None and not force:
        return _NODES
    try:
        s = open(_BASE, encoding="utf-8").read()
    except OSError:
        _NODES, _ORDERED = {}, []
        return _NODES
    reg, _ = _sidebar_region(s)
    com = [(m.start(), m.end()) for m in re.finditer(r"\{#.*?#\}", reg, re.S)]
    in_com = lambda p: any(x <= p < y for x, y in com)  # noqa: E731
    marks = [(m.start(), m.group()) for m in re.finditer(r"<ul\b|</ul>", reg) if not in_com(m.start())]
    nodes, order = {}, []
    section, accordion = "_top", None
    idx = 0
    lis = [m for m in re.finditer(r"<li\b[^>]*>", reg) if not in_com(m.start())]
    for k, m in enumerate(lis):
        depth = sum(1 if t == "<ul" else -1 for p, t in marks if p < m.start())
        full = reg[m.start():lis[k + 1].start() if k + 1 < len(lis) else len(reg)]
        nid = node_id_for(reg[m.end():], reg[m.start():m.start() + 400])
        if not nid or nid in nodes:
            continue
        idx += 1
        lk = re.search(r"sidebar_label\('([a-z0-9_]+)',\s*'([^']*)'\)", full[:900])
        if nid.startswith("sec:"):
            label, key = _title(nid[4:]), None
            section, accordion = nid, None
            group, level = "_sections", 0
        else:
            if lk:
                key, label = lk.group(1), lk.group(2)
            else:
                key = None
                a = re.search(r"<a\b[^>]*>(.*?)</a>", full, re.S)
                txt = re.sub(r"<span\b[^>]*>[^<]*</span>|\{%.*?%\}|\{\{.*?\}\}|<[^>]+>", " ", a.group(1) if a else "", flags=re.S)
                label = re.sub(r"\s+", " ", txt).strip() or nid
            if depth >= 2:     # inside an accordion's own <ul>
                group, level = (accordion or section), 1
            else:
                group, level = section, 0
                accordion = nid if nid.startswith("#") else None
        nodes[nid] = dict(id=nid, group=group, level=level, key=key, label=label, idx=idx, section=section)
        order.append(nid)
    _NODES, _ORDERED = nodes, order
    return _NODES


def default_order(nid):
    n = parse().get(nid)
    return n["idx"] * 1000 if n else 0


def _heading_order(group):
    if group == "_top":
        return -1000
    n = parse().get(group)
    return n["idx"] * 1000 if n else 0


def sb_order(nid: str) -> int:
    """CSS order for one sidebar <li> (Jinja global)."""
    nodes = parse()
    n = nodes.get(nid)
    if not n:
        return 0
    g = n["group"]
    saved = _ALIGN.get(g)
    if not saved or g == "_sections":
        return n["idx"] * 1000
    h = _heading_order(g)
    if nid in saved:
        return h + 1 + saved.index(nid)
    # an item added after the order was saved: after the saved ones, base.html order
    rank = [x for x in _ORDERED if nodes[x]["group"] == g and x not in saved].index(nid)
    return h + 500 + rank


def groups():
    """[(group id, title, [node, ...] in current order)] for the Sidebar
    Alignment tab: each section, then each accordion inside it."""
    from models.role_permissions import get_cached_sidebar_label
    nodes = parse()
    out, titles = [], {"_top": "Top (above the first section)"}
    for nid in _ORDERED:
        n = nodes[nid]
        if n["group"] == "_sections":
            titles[nid] = n["label"]
        elif nid.startswith("#"):
            titles[nid] = (get_cached_sidebar_label(n["key"], n["label"]) if n["key"] else n["label"])
    seen = []
    for nid in _ORDERED:
        g = nodes[nid]["group"]
        if g != "_sections" and g not in seen:
            seen.append(g)
    for g in seen:
        items = sorted((nodes[x] for x in _ORDERED if nodes[x]["group"] == g), key=lambda n: sb_order(n["id"]))
        rows = [dict(n, current=(get_cached_sidebar_label(n["key"], n["label"]) if n["key"] else n["label"]),
                     renamed=bool(n["key"]) and get_cached_sidebar_label(n["key"], n["label"]) != n["label"],
                     is_group=n["id"].startswith("#")) for n in items]
        sec = nodes[g]["section"] if g in nodes else g
        out.append(dict(id=g, title=titles.get(g, g), nested=g.startswith("#"),
                        section=_title(sec[4:]) if str(sec).startswith("sec:") else "", rows=rows,
                        custom=bool(_ALIGN.get(g))))
    return out


def effective_ranks() -> dict:
    """{module key: position} in the sidebar as it shows now (alignment
    applied): top links, then each section heading and its items; an
    accordion's own items right after it."""
    nodes = parse()
    by_order = lambda g: sorted((n for n in nodes.values() if n["group"] == g), key=lambda n: sb_order(n["id"]))  # noqa: E731
    flat = by_order("_top")
    for h in sorted((n for n in nodes.values() if n["group"] == "_sections"), key=lambda n: n["idx"]):
        flat += [h] + by_order(h["id"])
    ranks, i = {}, 0
    for n in flat:
        for x in [n] + (by_order(n["id"]) if n["id"].startswith("#") else []):
            i += 1
            if x["key"] and x["key"] not in ranks:
                ranks[x["key"]] = i
    return ranks


def set_alignment(data: dict):
    global _ALIGN
    nodes = parse()
    clean = {}
    for g, ids in (data or {}).items():
        if not isinstance(ids, list):
            continue
        ok = [i for i in ids if i in nodes and nodes[i]["group"] == g]
        if ok:
            clean[g] = list(dict.fromkeys(ok))
    _ALIGN = clean
    return clean


async def load_alignment_to_cache(db):
    from sqlalchemy import select
    from models.settings import AppSetting
    row = (await db.execute(select(AppSetting).where(AppSetting.key == SETTING_KEY))).scalar_one_or_none()
    try:
        set_alignment(json.loads(row.value) if row and row.value else {})
    except (ValueError, TypeError):
        set_alignment({})


def current_alignment() -> dict:
    return dict(_ALIGN)
