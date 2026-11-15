#!/usr/bin/env python3
"""Genera una página HTML con el índice de acumulación de BTC.

Uso:  python actualizar.py [ruta_de_salida.html]
Necesita:  pip install requests pytrends   (pytrends es opcional)

Todas las fuentes son públicas y no piden clave. Si una falla, ese componente
se muestra como "sin datos" y el índice se calcula con los demás.
"""
import bisect
import csv
import datetime as dt
import io
import json
import sys
import time
from functools import lru_cache
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; indice-acumulacion/1.0)"}
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("index.html")


def get(url, intentos=3, **kw):
    """GET con 3 intentos y 60 s de espera máxima (FRED a veces responde lento)."""
    for i in range(intentos):
        try:
            r = requests.get(url, headers=UA, timeout=60, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if i == intentos - 1:
                raise
            time.sleep(5 * (i + 1))


def nota(x):
    """Redondea y limita a 0-10."""
    return int(max(0, min(10, x)) + 0.5)


def miles(x):
    return f"{x:,.0f}".replace(",", ".")


# ---------------------------------------------------------------------------
# REGLAS DE PUNTUACIÓN (0-10). Edita las fórmulas de cada función a tu gusto.
# Criterio general: más miedo / más sobrevendido / dólar más débil = más nota.
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def cierres_btc():
    """Cierres diarios de BTC/USD (Kraken, últimas ~720 velas, sin clave)."""
    r = get("https://api.kraken.com/0/public/OHLC",
            params={"pair": "XBTUSD", "interval": 1440}).json()["result"]
    clave = next(k for k in r if k != "last")
    return [float(c[4]) for c in r[clave]]


def fear_greed():
    datos = get("https://api.alternative.me/fng/?limit=7").json()["data"]
    vals = [int(x["value"]) for x in datos]
    ahora, media = vals[0], sum(vals) / len(vals)
    return nota((100 - ahora) / 10), f"FG actual: {ahora} | 7d avg: {media:.1f}"


def rsi_diario(n=14):
    c = cierres_btc()
    ganan = sum(max(c[i] - c[i - 1], 0) for i in range(1, n + 1)) / n
    pierden = sum(max(c[i - 1] - c[i], 0) for i in range(1, n + 1)) / n
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        ganan = (ganan * (n - 1) + max(d, 0)) / n
        pierden = (pierden * (n - 1) + max(-d, 0)) / n
    rsi = 100 if pierden == 0 else 100 - 100 / (1 + ganan / pierden)
    return nota((70 - rsi) / 5), f"RSI 14d: {rsi:.1f}"   # RSI 70 -> 0, RSI 20 -> 10


def ema200():
    c = cierres_btc()
    k = 2 / 201
    ema = sum(c[:200]) / 200
    for v in c[200:]:
        ema = v * k + ema * (1 - k)
    dev = (c[-1] / ema - 1) * 100
    return nota(5 - dev / 4), f"EMA200d: ${miles(ema)} ({dev:+.1f}%)"   # 20% por debajo -> 10


def dxy():
    j = get("https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB",
            params={"range": "3mo", "interval": "1d"}).json()
    c = [x for x in j["chart"]["result"][0]["indicators"]["quote"][0]["close"] if x]
    cambio = (c[-1] / c[-22] - 1) * 100 if len(c) > 22 else 0.0
    return nota(5 - cambio * 2), f"DXY: {c[-1]:.2f} | 1m: {cambio:+.2f}%"   # dólar sube -> baja nota


def fred(serie):
    desde = (dt.date.today() - dt.timedelta(days=800)).isoformat()   # solo ~2 años: mucho más rápido
    txt = get("https://fred.stlouisfed.org/graph/fredgraph.csv",
              params={"id": serie, "cosd": desde}).text
    filas = list(csv.reader(io.StringIO(txt)))[1:]
    return {f[0]: float(f[1]) for f in filas if len(f) > 1 and f[1] not in ("", ".")}


def liquidez():
    """Liquidez neta de la Fed = balance - cuenta del Tesoro - repos inversos (en millones USD)."""
    walcl, tga, rrp = fred("WALCL"), fred("WTREGEN"), fred("RRPONTSYD")
    kt, kr = sorted(tga), sorted(rrp)

    def asof(keys, s, d):
        i = bisect.bisect_right(keys, d) - 1
        return s[keys[i]] if i >= 0 else 0.0

    neta = [walcl[d] - asof(kt, tga, d) - asof(kr, rrp, d) * 1000 for d in sorted(walcl)][-80:]
    var13 = [neta[i] / neta[i - 13] - 1 for i in range(13, len(neta))][-52:]
    actual = var13[-1]
    pct = 100 * sum(1 for v in var13 if v <= actual) / len(var13)
    return nota((100 - pct) / 10), f"Liquidez neta P{pct:.0f} (var 13s vs 1a)"   # poca liquidez -> más nota


def interes():
    from pytrends.request import TrendReq   # no oficial: puede fallar desde servidores
    p = TrendReq(hl="es-ES", tz=60)
    p.build_payload(["bitcoin"], timeframe="today 3-m")
    v = int(p.interest_over_time()["bitcoin"].iloc[-1])
    return nota((100 - v) / 10), f"Valor: {v}/100"   # poco interés -> más nota


COMPONENTES = [
    ("F", "Fear & Greed", "#f5a623", fear_greed),
    ("R", "RSI diario", "#ff7a1a", rsi_diario),
    ("E", "EMA 200D", "#3cc8ff", ema200),
    ("L", "Liquidez", "#ff4d5e", liquidez),
    ("D", "Dollar DXY", "#12d68f", dxy),
    ("I", "Interés", "#b45cff", interes),
]


def calcular():
    comps = []
    for k, n, c, fn in COMPONENTES:
        try:
            s, d = fn()
        except Exception as e:
            s, d = None, f"sin datos ({type(e).__name__})"
            print(f"[aviso] {n}: {e}", file=sys.stderr)
        comps.append({"k": k, "n": n, "c": c, "s": s, "d": d})
    ok = [x["s"] for x in comps if x["s"] is not None]
    indice = round(sum(ok) / (10 * len(ok)) * 100) if ok else None
    return {"comps": comps, "index": indice,
            "updated": dt.datetime.now(dt.timezone.utc).isoformat()}


PLANTILLA = r"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Índice de acumulación</title>
<style>
:root{box-sizing:border-box;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px);color-scheme:dark;
--bg:#07080c;--panel:#0f1118;--card:#090a0f;--line:#1c1f2b;--text:#e8eaf2;--muted:#8a90a6;--track:#232634}
@media (prefers-color-scheme:light){:root{color-scheme:light;--bg:#eef0f5;--panel:#fff;--card:#f5f6fa;--line:#dcdfe9;--text:#161826;--muted:#5d6379;--track:#d9dce8}}
*,*::before,*::after{box-sizing:inherit}
html{height:100%}body{margin:0;min-height:100%;background:var(--bg);color:var(--text);font-family:"Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif;padding:16px}
.wrap{max-width:1200px;margin:0 auto;display:grid;gap:16px}
.banner{background:linear-gradient(90deg,#fff200,#ffd400);color:#111;border-radius:18px;padding:22px 28px;display:flex;align-items:center;gap:22px}
.logo{flex:none;width:72px;height:72px;border-radius:50%;border:3px solid #111;display:grid;place-items:center;font-size:34px;font-weight:900}
.banner h1{margin:0;font-size:1.7rem;font-weight:800}.banner p{margin:4px 0 0;font-weight:600}
.main{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,2fr);gap:16px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:22px}
.panel h2{margin:0 0 18px;font-size:.8rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-weight:700}
.score{text-align:center;padding-top:14px}.num{font-size:7rem;font-weight:900;line-height:1}
.of{color:var(--muted);margin-top:4px}
.state{display:flex;align-items:center;justify-content:center;gap:10px;margin:18px 0 34px;font-size:1.5rem;font-weight:800}
.dot{width:24px;height:24px;border-radius:50%;background:currentColor;box-shadow:0 0 14px currentColor}
.gauge{position:relative;height:24px;border-radius:12px;background:linear-gradient(90deg,#ff4d5e,#ff9a3c 30%,#ffd400 55%,#12d68f 90%)}
.gauge i{position:absolute;top:-5px;width:6px;height:34px;border-radius:3px;background:#fff;box-shadow:0 0 0 1px rgba(0,0,0,.35);transform:translateX(-50%)}
.scale{display:flex;justify-content:space-between;margin-top:12px;font-size:.85rem;color:var(--muted);text-align:center}.scale b{display:block;font-weight:400}
.upd{text-align:center;color:var(--muted);margin-top:28px;font-size:.95rem}
.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;text-align:center}
.card .l{font-size:2.6rem;font-weight:900;line-height:1}.card .n{margin-top:6px;font-size:.85rem;color:var(--muted)}
.card .s{margin:6px 0 10px;font-size:1.25rem;font-weight:800}
.bar{height:6px;border-radius:3px;background:var(--track);overflow:hidden}.bar span{display:block;height:100%;background:#12d68f}
.card .d{margin-top:10px;font-size:.82rem;color:var(--muted)}
@media (max-width:880px){.main{grid-template-columns:1fr}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.num{font-size:5.5rem}}
@media (max-width:520px){.cards{grid-template-columns:1fr}.banner{padding:18px}.logo{width:56px;height:56px;font-size:26px}.banner h1{font-size:1.3rem}}
</style></head><body><div class="wrap">
<header class="banner"><div class="logo" aria-hidden="true">A</div><div><h1>Índice de acumulación</h1><p>BTC · datos públicos</p></div></header>
<main class="main">
<section class="panel"><h2>Índice general</h2>
<div class="score"><div class="num" id="num">—</div><div class="of">/100</div>
<div class="state" id="state"><span class="dot"></span><span id="label"></span></div></div>
<div class="gauge"><i id="marker" style="left:0%"></i></div>
<div class="scale"><span>0<b>Techo</b></span><span>25</span><span>50<b>Neutral</b></span><span>75</span><span>100<b>Suelo</b></span></div>
<div class="upd" id="upd"></div></section>
<section class="panel"><h2>Componentes</h2><div class="cards" id="cards"></div></section>
</main></div>
<script>
const D=__DATA__;
const zonas=[[80,"Acumulación fuerte"],[60,"Acumulación"],[40,"Neutral"],[20,"Precaución"],[0,"Techo: distribuir"]];
const cards=document.getElementById("cards");
D.comps.forEach(c=>{const el=document.createElement("div");el.className="card";
el.innerHTML=`<div class="l" style="color:${c.c}">${c.k}</div><div class="n">${c.n}</div><div class="s">${c.s===null?"—":c.s+"/10"}</div><div class="bar"><span style="width:${(c.s||0)*10}%"></span></div><div class="d"></div>`;
el.querySelector(".d").textContent=c.d;cards.appendChild(el);});
const i=D.index;
if(i!==null){const col=`hsl(${Math.round(i*1.4)},78%,50%)`;
const n=document.getElementById("num");n.textContent=i;n.style.color=col;
document.getElementById("state").style.color=col;
document.getElementById("label").textContent=zonas.find(z=>i>=z[0])[1];
document.getElementById("marker").style.left=i+"%";}
else{document.getElementById("label").textContent="Sin datos";}
const f=new Date(D.updated),p=x=>String(x).padStart(2,"0");
document.getElementById("upd").textContent=`Actualizado: ${p(f.getDate())}/${p(f.getMonth()+1)} ${p(f.getHours())}:${p(f.getMinutes())}`;
</script></body></html>
"""


def main():
    datos = calcular()
    html = PLANTILLA.replace("__DATA__", json.dumps(datos, ensure_ascii=False).replace("</", "<\\/"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"Índice: {datos['index']} -> {OUT}")


if __name__ == "__main__":
    main()
