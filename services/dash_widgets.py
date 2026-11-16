"""Dashboard widgets for the Option C page dashboards (2026-11-14, Pankaj).

Each builder returns a plain dict that templates/_dash.html draws (chart
geometry is worked out here, so templates stay simple). Light cards take their
colours from CSS variables in static/css/dash.css, so they follow the user's
light / dark theme. Ported from the design canvas generator.
"""
import math, random

C = dict(blue='#2563eb', cyan='#0891b2', green='#059669', amber='#d97706', red='#e11d48', violet='#7c3aed',
         slate='#64748b', pink='#db2777', teal='#0d9488', indigo='#4f46e5', navy='#0e3a7a', orange='#ea580c',
         lime='#65a30d', sky='#0284c7', grey='#cbd5e1')
GRAD = dict(blue=('#0e3a7a', '#1d4ed8'), cyan=('#0369a1', '#0891b2'), green=('#047857', '#059669'),
            red=('#be123c', '#e11d48'), violet=('#6d28d9', '#9333ea'), amber=('#b45309', '#d97706'),
            teal=('#0f766e', '#14b8a6'), slate=('#1e293b', '#475569'), pink=('#9d174d', '#db2777'),
            indigo=('#3730a3', '#6366f1'), navy=('#0b1f44', '#0e3a7a'), orange=('#c2410c', '#ea580c'))
MONTHS = ['Dec', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov']
DAYS14 = ['%d Nov' % d for d in range(1, 15)]
HOURS = ['9a', '10a', '11a', '12p', '1p', '2p', '3p', '4p', '5p', '6p']
WEEKS8 = ['W38', 'W39', 'W40', 'W41', 'W42', 'W43', 'W44', 'W45']


def c(x):
    return C.get(x, x)


def fmt(v, k='n'):
    if k == 'inr':
        if v >= 1e7: return '₹%.2fCr' % (v / 1e7)
        if v >= 1e5: return '₹%.1fL' % (v / 1e5)
        if v >= 1e3: return '₹%.0fK' % (v / 1e3)
        return '₹%d' % round(v)
    if k == 'pct': return '%.0f%%' % v
    if k == 'd': return '%.1fd' % v
    if k == 'h': return '%.1fh' % v
    if v >= 1e5: return '%.1fL' % (v / 1e5)
    if v >= 1e4: return '%.1fK' % (v / 1e3)
    return '{:,}'.format(int(round(v)))


def nice(m):
    if m <= 0: return 1
    e = 10 ** math.floor(math.log10(m)); f = m / e
    for s in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if f <= s + 1e-9: return s * e
    return 10 * e


def ser(seed, n, base, trend=0.0, noise=0.15, season=0.0):
    r = random.Random(seed); out = []
    for i in range(n):
        t = i / (n - 1) if n > 1 else 0
        v = base * (1 + trend * t) * (1 + season * math.sin(t * math.pi * 3)) * (1 + r.uniform(-noise, noise))
        out.append(max(0.0, v))
    return out


def _pts(vals, mx, W=600, H=200, pad=6):
    n = len(vals)
    return [(i * W / (n - 1), H - pad - (v / mx) * (H - 2 * pad)) for i, v in enumerate(vals)]


def _cr(p):
    d = 'M%.1f %.1f' % p[0]
    for i in range(len(p) - 1):
        p0 = p[i - 1] if i > 0 else p[i]; p1 = p[i]; p2 = p[i + 1]; p3 = p[i + 2] if i + 2 < len(p) else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d += ' C%.1f %.1f %.1f %.1f %.1f %.1f' % (c1 + c2 + p2)
    return d


def _area(p, H=200):
    return _cr(p) + ' L%.1f %d L0 %d Z' % (p[-1][0], H, H)


def spark(vals, W=120, H=36):
    mn, mx = min(vals), max(vals)
    rng = (mx - mn) or 1
    p = [(i * W / (len(vals) - 1), H - 3 - (v - mn) / rng * (H - 8)) for i, v in enumerate(vals)]
    return _cr(p), _area(p, H)


def tint(col, a):
    return col + '%02x' % max(0, min(255, int(a * 255)))


LIGHT = dict(cbg='var(--dk-card)', cbd='var(--dk-border)', tfg='var(--dk-text)', mut='var(--dk-mut)', grid='var(--dk-grid)', hole='var(--dk-card)', track='var(--dk-track)')
DARK = dict(cbg='#0f172a', cbd='#0f172a', tfg='#f8fafc', mut='#94a3b8', grid='rgba(255,255,255,0.10)', hole='#0f172a',
            track='rgba(255,255,255,0.08)')
KINDS = ['Slim', 'Area', 'Bars', 'Hbars', 'Donut', 'Gauge', 'Stack', 'Table', 'Funnel', 'Heat', 'Lanes', 'Rings', 'Prog', 'Feed',
         'Cal', 'Strip', 'Insight', 'Tiles', 'Pipe', 'Kv']


def base(kind, title, span, sub='', tag='', dark=False, tagc='blue', href=''):
    d = dict(DARK if dark else LIGHT)
    for k in KINDS: d['is' + k] = (k == kind)
    tc = c(tagc)
    d.update(title=title, sub=sub, tag=tag, hasTag=bool(tag), span=span, h=0, bh=0,
             href=href, tagbg=('rgba(255,255,255,0.12)' if dark else tint(tc, 0.1)), tagfg=('#e2e8f0' if dark else tc),
             leg=[], yt=[], xl=[], p1='', p2='', p3='', a1='', a2='', a3='', c1=C['blue'], c2=C['slate'], c3=C['amber'],
             fo='0.14', dash2='none', groups=[], ggap='10px', bmax='28px', rows=[], lw='120px',
             conic='#e5e7eb 0 100%', center='', centerSub='', dsz='150px', dash='0 282.7', gval='', gsub='',
             gc=C['blue'], mini=[], big=[], segs=[], gtc='1fr', cols=[], trs=[], steps=[], hcols=[], hrows=[],
             lanes=[], ln=4, rings=[], rc=3, pitems=[], fitems=[], cells=[], calCols=20, calL='', scale=[],
             stats=[], sc=4, ins=[], btn='', hasBtn=False, btnml='auto', btnbd='var(--dk-border)', btnfg='var(--dk-text)', btnbg='var(--dk-card)', btnhref='', btnmodal='', pts=[], hpad='0%', donutTip='', tiles=[], tc=6, tvs='22px', pipe=[], kv=[], kvBig='', hasBig=False, foot='', slim=[])
    return d


def withbtn(w, text, href='', modal=''):
    w['btn'] = text; w['hasBtn'] = True; w['btnml'] = '0' if w['hasTag'] else 'auto'
    w['btnhref'] = href; w['btnmodal'] = modal
    if w['cbg'] == DARK['cbg']: w.update(btnbd='#334155', btnfg='#e2e8f0', btnbg='transparent')
    return w


def _xl(labels, maxn=8):
    if len(labels) <= maxn: return [{'x': x} for x in labels]
    step = math.ceil(len(labels) / maxn)
    return [{'x': x} for i, x in enumerate(labels) if i % step == 0 or i == len(labels) - 1][:maxn + 1]


def area(title, span, labels, series, kind='n', stacked=False, dashed=False, legv='last', **kw):
    d = base('Area', title, span, **kw)
    vals = [s[1] for s in series]
    if stacked:
        cum = []; run = [0] * len(labels)
        for v in vals:
            run = [a + b for a, b in zip(run, v)]; cum.append(run)
        draw = cum; mx = nice(max(cum[-1]))
    else:
        draw = vals; mx = nice(max(max(v) for v in vals))
    d['yt'] = [{'x': fmt(mx * (4 - i) / 4, kind)} for i in range(5)]
    d['xl'] = _xl(labels)
    for i, (s, v) in enumerate(zip(series, draw)):
        p = _pts(v, mx)
        d['p%d' % (i + 1)] = _cr(p); d['c%d' % (i + 1)] = c(s[2])
        if stacked or i == 0: d['a%d' % (i + 1)] = _area(p)
    if stacked: d['fo'] = '0.55'
    if dashed: d['dash2'] = '6 5'
    d['leg'] = [{'n': s[0], 'c': c(s[2]), 'v': fmt(sum(s[1]) if legv == 'sum' else s[1][-1], kind)} for s in series]
    d['pts'] = [{'tip': '%s — %s' % (labels[i], ' · '.join('%s: %s' % (s[0], fmt(s[1][i], kind)) for s in series))}
                for i in range(len(labels))]
    d['hpad'] = '%.4f%%' % (50.0 / max(1, len(labels) - 1))   # hover columns centred on each point
    return d


def bars(title, span, labels, series, kind='n', stacked=False, bmax='28px', legv='sum', **kw):
    d = base('Bars', title, span, **kw)
    vals = [s[1] for s in series]
    if stacked:
        tot = [sum(x) for x in zip(*vals)]; mx = nice(max(tot))
        d['groups'] = [{'l': l, 'stacks': [{'h': '%.1f%%' % (tot[i] / mx * 100),
                                            'segs': [{'f': '%.3f' % (v[i] or 0.001), 'c': c(s[2]),
                                                      'tip': '%s · %s: %s' % (l, s[0], fmt(v[i], kind))} for s, v in zip(series, vals)]}]}
                       for i, l in enumerate(labels)]
    else:
        mx = nice(max(max(v) for v in vals))
        d['groups'] = [{'l': l, 'stacks': [{'h': '%.1f%%' % (v[i] / mx * 100),
                                            'segs': [{'f': '1', 'c': c(s[2]), 'tip': '%s · %s: %s' % (l, s[0], fmt(v[i], kind))}]}
                                           for s, v in zip(series, vals)]} for i, l in enumerate(labels)]
    d['yt'] = [{'x': fmt(mx * (4 - i) / 4, kind)} for i in range(5)]
    d['bmax'] = bmax
    d['ggap'] = '6px' if len(labels) > 10 else '10px'
    d['leg'] = [{'n': s[0], 'c': c(s[2]), 'v': (fmt(sum(s[1]), kind) if legv == 'sum' else '')} for s in series]
    return d


def waterfall(title, span, steps, kind='inr', **kw):
    """steps: (label, value, 'total'|'up'|'down')"""
    d = base('Bars', title, span, **kw)
    run = 0; items = []
    for l, v, t in steps:
        if t == 'total':
            items.append((l, 0, v, C['navy'])); run = v
        elif t == 'down':
            run -= v; items.append((l, run, v, C['red']))
        else:
            items.append((l, run, v, C['green'])); run += v
    mx = nice(max(b + v for _, b, v, _ in items))
    d['groups'] = [{'l': l, 'stacks': [{'h': '%.1f%%' % ((b + v) / mx * 100),
                                        'segs': [{'f': '%.3f' % (b or 0.0001), 'c': 'transparent', 'tip': ''},
                                                 {'f': '%.3f' % v, 'c': col, 'tip': '%s: %s' % (l, fmt(v, kind))}]}]}
                   for l, b, v, col in items]
    d['yt'] = [{'x': fmt(mx * (4 - i) / 4, kind)} for i in range(5)]
    d['bmax'] = '54px'
    d['leg'] = [{'n': 'Total', 'c': C['navy'], 'v': ''}, {'n': 'Adds', 'c': C['green'], 'v': ''}, {'n': 'Cost', 'c': C['red'], 'v': ''}]
    return d


def hbars(title, span, rows, kind='n', color='blue', colors=None, lw='120px', **kw):
    d = base('Hbars', title, span, **kw)
    mx = max(abs(r[1]) for r in rows) or 1
    d['rows'] = [{'l': r[0], 'w': '%.1f%%' % (abs(r[1]) / mx * 100), 'v': (r[2] if len(r) > 2 else fmt(r[1], kind)),
                  'tip': '%s: %s' % (r[0], r[2] if len(r) > 2 else fmt(r[1], kind)),
                  'c': c(colors[i % len(colors)] if colors else color)} for i, r in enumerate(rows)]
    d['lw'] = lw
    return d


def donut(title, span, parts, center, centerSub, kind='n', **kw):
    d = base('Donut', title, span, **kw)
    tot = sum(p[1] for p in parts); a = 0; segs = []
    for p in parts:
        b = a + p[1] / tot * 100
        segs.append('%s %.2f%% %.2f%%' % (c(p[2]), a, b)); a = b
    d['conic'] = ', '.join(segs)
    d['center'] = center; d['centerSub'] = centerSub
    d['leg'] = [{'n': p[0], 'c': c(p[2]), 'v': fmt(p[1], kind), 'p': '%.0f%%' % (p[1] / tot * 100),
                 'tip': '%s: %s (%.0f%%)' % (p[0], fmt(p[1], kind), p[1] / tot * 100)} for p in parts]
    d['donutTip'] = ' · '.join('%s %s' % (p[0], fmt(p[1], kind)) for p in parts)
    return d


def gauge(title, span, pct, val, gsub, color='blue', mini=(), **kw):
    d = base('Gauge', title, span, **kw)
    d['dash'] = '%.1f 282.7' % (max(0, min(100, pct)) / 100 * 282.7)
    d['gval'] = val; d['gsub'] = gsub; d['gc'] = c(color)
    d['mini'] = [{'l': m[0], 'v': m[1]} for m in mini]
    return d


def stack(title, span, parts, kind='n', **kw):
    d = base('Stack', title, span, **kw)
    tot = sum(p[1] for p in parts)
    d['big'] = [{'l': p[0], 'v': fmt(p[1], kind), 'c': c(p[2]), 'p': '%.0f%%' % (p[1] / tot * 100)} for p in parts]
    d['segs'] = [{'w': '%.2f%%' % (p[1] / tot * 100), 'c': c(p[2]), 'tip': '%s: %s (%.0f%%)' % (p[0], fmt(p[1], kind), p[1] / tot * 100)}
                 for p in parts]
    return d


def table(title, span, cols, rows, **kw):
    d = base('Table', title, span, **kw)
    d['gtc'] = ' '.join('minmax(0, %sfr)' % cc[1] for cc in cols)
    d['cols'] = [{'x': cc[0]} for cc in cols]
    trs = []
    for r in rows:
        cells = []
        for j, x in enumerate(r):
            cell = {'x': x, 'b': False, 'nb': False, 'lk': False, 'bt2': False, 'bc': '', 'bt': '', 'fw': '600' if j == 0 else '400',
                    'href': '', 'modal': '', 'data': {}}
            if isinstance(x, dict):
                cell.update(x=x.get('x', ''), href=x.get('href', ''), modal=x.get('modal', ''), data=x.get('data', {}))
                cell['bt2' if x.get('kind') == 'btn' else 'lk'] = True
            elif isinstance(x, tuple) and x[1] == 'link':
                cell.update(x=x[0], lk=True)
            elif isinstance(x, tuple) and x[1] == 'btn':
                cell.update(x=x[0], bt2=True)
            elif isinstance(x, tuple):
                col = c(x[1]); cell.update(x=x[0], b=True, bc=tint(col, 0.13), bt=col, fw='600')
            else:
                cell['nb'] = True
            cells.append(cell)
        trs.append({'cells': cells})
    d['trs'] = trs
    return d


def funnel(title, span, steps, color='blue', kind='n', lw='110px', **kw):
    d = base('Funnel', title, span, **kw)
    top = steps[0][1]; col = c(color); out = []
    for i, s in enumerate(steps):
        a = 1 - i * 0.5 / max(1, len(steps) - 1)
        out.append({'l': s[0], 'v': fmt(s[1], kind), 'w': '%.1f%%' % max(22, s[1] / top * 100),
                    'tip': '%s: %s%s' % (s[0], fmt(s[1], kind), '' if i == 0 else ' (%.0f%% of previous)' % (s[1] / steps[i - 1][1] * 100 if steps[i - 1][1] else 0)),
                    'c': tint(col, a), 'p': '' if i == 0 else '↓ %.0f%%' % (s[1] / steps[i - 1][1] * 100)})
    d['steps'] = out; d['lw'] = lw
    return d


def heat(title, span, cols, rows, color='blue', showv=True, kind='n', lw='110px', **kw):
    d = base('Heat', title, span, **kw)
    col = c(color)
    mx = max(max(r[1]) for r in rows) or 1
    d['hcols'] = [{'x': x} for x in cols]
    hr = []
    for r in rows:
        cells = []
        for ci, v in enumerate(r[1]):
            a = 0.08 + 0.92 * (v / mx)
            cells.append({'bg': tint(col, a), 'fg': '#ffffff' if a > 0.55 else 'var(--dk-text)', 'v': fmt(v, kind) if showv else '',
                          'tip': '%s · %s: %s' % (r[0], cols[ci] if ci < len(cols) else '', fmt(v, kind))})
        hr.append({'l': r[0], 'cells': cells})
    d['hrows'] = hr; d['lw'] = lw
    return d


def lanes(title, span, ls, **kw):
    d = base('Lanes', title, span, **kw)
    mx = max(l[2] for l in ls) or 1
    out = []
    for l in ls:
        tgt = l[4] if len(l) > 4 else None
        out.append({'n': l[0], 'c': c(l[1]), 'k': fmt(l[2]),
                    'w': '%.0f%%' % (min(1, tgt[0] / tgt[1]) * 100 if tgt else l[2] / mx * 100),
                    'hasT': bool(tgt), 't': ('Done today %d / target %d' % tgt) if tgt else '',
                    'chips': [{'t': ch[0], 's': ch[1]} for ch in l[3]]})
    d['lanes'] = out
    d['ln'] = len(ls)
    return d


def rings(title, span, items, cols=3, **kw):
    d = base('Rings', title, span, **kw)
    d['rings'] = [{'l': i[0], 'v': i[3] if len(i) > 3 else '%d%%' % i[1], 'c': c(i[2]) if i[1] else 'transparent',
                   'dash': '%.1f 138.2' % (i[1] / 100 * 138.2)} for i in items]
    d['rc'] = cols
    return d


def prog(title, span, items, **kw):
    d = base('Prog', title, span, **kw)
    d['pitems'] = [{'l': i[0], 'v': i[1], 'w': '%.0f%%' % i[2], 'c': c(i[3])} for i in items]
    return d


def feed(title, span, items, **kw):
    d = base('Feed', title, span, **kw)
    d['fitems'] = [{'t': i[0], 's': i[1], 'time': i[2], 'c': c(i[3])} for i in items]
    return d


def cal(title, span, weeks, seed, color='green', label='', **kw):
    d = base('Cal', title, span, **kw)
    col = c(color); r = random.Random(seed); cells = []
    for i in range(weeks * 7):
        dow = i % 7
        v = 0 if dow == 6 and r.random() < 0.8 else r.random() ** 1.3
        cells.append({'bg': tint(col, 0.08 + 0.85 * v)})
    d['cells'] = cells; d['calCols'] = weeks; d['calL'] = label
    d['scale'] = [{'bg': tint(col, a)} for a in (0.08, 0.3, 0.5, 0.7, 0.93)]
    return d


def strip(title, span, stats, **kw):
    d = base('Strip', title, span, **kw)
    out = []
    for s in stats:
        p, _ = spark(s[4], 100, 30)
        good = s[3]
        out.append({'l': s[0], 'v': s[1], 'd': s[2], 'dc': C['green'] if good else C['red'], 'p': p,
                    'c': c(s[5]) if len(s) > 5 else C['blue']})
    d['stats'] = out; d['sc'] = len(stats)
    return d


def insight(title, span, items, **kw):
    d = base('Insight', title, span, **kw)
    d['ins'] = [{'ic': i[0], 't': i[1], 'c': c(i[2]), 'bg': tint(c(i[2]), 0.12)} for i in items]
    return d



def tiles(title, span, items, cols=6, light=False, vs='22px', **kw):
    """items: (label, value, color, [sub lines])"""
    d = base('Tiles', title, span, **kw)
    out = []
    for it in items:
        col = c(it[2]); subs = it[3] if len(it) > 3 else []
        dim = str(it[1]).strip() in ('0',)
        if light:
            out.append(dict(l=it[0], v=it[1], bg=tint(col, 0.08), fg=col, bl=col, lc='#475569', op='0.55' if dim else '1', subs=[{'x': x} for x in subs]))
        else:
            out.append(dict(l=it[0], v=it[1], bg='linear-gradient(135deg, %s, %s)' % (col, tint(col, 0.82)), fg='#ffffff', bl=col, lc='#ffffff',
                            op='0.6' if dim else '1', subs=[{'x': x} for x in subs]))
    d['tiles'] = out; d['tc'] = cols; d['tvs'] = vs
    return d


def pipe(title, span, stages, **kw):
    """stages: (label, value, header colour, sub text, sub colour)"""
    d = base('Pipe', title, span, **kw)
    d['pipe'] = [dict(l=s[0], v=s[1], hc=c(s[2]), s=s[3] if len(s) > 3 else '', sc=c(s[4]) if len(s) > 4 else '#64748b',
                      ao='0' if i == len(stages) - 1 else '1') for i, s in enumerate(stages)]
    return d


def slim(title, span, stages, **kw):
    """Slim one-line pipeline (2026-11-16, Product Master): (label, value, colour, sub, tip)."""
    d = base('Slim', title, span, **kw)
    d['slim'] = [dict(l=s[0], v=s[1], c=c(s[2]), s=s[3] if len(s) > 3 else '', tip=s[4] if len(s) > 4 else '',
                      bg=tint(c(s[2]), 0.08), bd=tint(c(s[2]), 0.32), ao='0' if i == len(stages) - 1 else '1')
                 for i, s in enumerate(stages)]
    return d


def kv(title, span, rows, big='', foot='', **kw):
    """rows: (label, value, dot colour or '', value colour or '', bold)"""
    d = base('Kv', title, span, **kw)
    out = []
    for i, r in enumerate(rows):
        bold = len(r) > 4 and r[4]
        out.append(dict(l=r[0], v=r[1], dc=c(r[2]) if len(r) > 2 and r[2] else 'transparent', c=c(r[3]) if len(r) > 3 and r[3] else d['tfg'],
                        fw='700' if bold else '500', fs='15px' if bold else '13px', bt=d['grid'] if i else 'transparent'))
    d['kv'] = out; d['kvBig'] = big; d['hasBig'] = bool(big); d['foot'] = foot
    return d


def kpi(label, value, delta='', good=True, color='blue', trend=None, style='gradient'):
    """A KPI tile. trend: list of numbers for the sparkline (optional)."""
    p = a = ''
    if trend and len(trend) > 1 and max(trend) != min(trend):
        p, a = spark(trend)
    if style == 'gradient':
        k1, k2 = GRAD.get(color, GRAD['blue'])
        return dict(l=label, v=value, d=delta, bg='linear-gradient(135deg, %s, %s)' % (k1, k2), bd=k1, fg='#ffffff',
                    dbg='rgba(255,255,255,0.18)', dfg='#ffffff', sc='#ffffff', p=p, a=a)
    col = C.get(color, color)
    return dict(l=label, v=value, d=delta, bg='var(--dk-card)', bd='var(--dk-border)', fg='var(--dk-text)',
                dbg='rgba(22,163,74,.14)' if good else 'rgba(225,29,72,.13)', dfg='#15803d' if good else '#be123c', sc=col, p=p, a=a)


def layout(rows):
    """rows: [(height_px, [widget, ...]), ...] -> flat widget list with heights set."""
    out = []
    for h, ws in rows:
        for w in ws:
            w['h'] = h; w['bh'] = h - 32 - 38 - 12 - 2
            if w.get('isDonut'): w['dsz'] = '%dpx' % min(w['bh'], 170)
            out.append(w)
    return out


def inr(v):
    """Indian money: ₹1.23Cr / ₹4.5L / ₹12K / ₹950."""
    v = v or 0
    return ('−' + fmt(-v, 'inr')) if v < 0 else fmt(v, 'inr')


def num(v):
    return '{:,}'.format(int(round(v or 0)))
