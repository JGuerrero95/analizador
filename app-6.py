import html as hmod
import io
import json
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
import datetime as dt

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

DB = "historial.db"
M = 1e6
FLUJO = {
    "Ventas": (["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"], M),
    "EBIT": (["OperatingIncomeLoss"], M),
    "Neto": (["NetIncomeLoss"], M),
    "CFO": (["NetCashProvidedByUsedInOperatingActivities"], M),
    "Capex": (["PaymentsToAcquirePropertyPlantAndEquipment"], M),
    "DA": (["DepreciationDepletionAndAmortization", "DepreciationAndAmortization"], M),
    "EPS": (["EarningsPerShareDiluted"], 1),
    "Acc": (["WeightedAverageNumberOfDilutedSharesOutstanding"], M),
    "Div": (["CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"], 1),
}
SALDO = {
    "Caja": (["CashAndCashEquivalentsAtCarryingValue"], M),
    "Deuda": (["LongTermDebt", "LongTermDebtNoncurrent"], M),
    "Patrimonio": (["StockholdersEquity"], M),
}

CSS = """
body{font-family:Georgia,serif;color:#14213d;margin:0;padding:12px;background:#fff}
h1{font-size:26px;margin:0}h2{font-size:12px;letter-spacing:.12em;text-transform:uppercase;border-bottom:1px solid #ccc;padding-bottom:4px;margin:18px 0 8px;font-family:Arial,sans-serif}
.t{font-size:18px;font-weight:bold;border-top:2px solid #14213d;margin:8px 0 12px;padding-top:8px}
.g{display:grid;grid-template-columns:1fr 1fr;gap:20px}@media(max-width:700px){.g{grid-template-columns:1fr}}
.card{background:#14213d;color:#fff;padding:14px;border-radius:4px}.card .v{font-size:34px;font-weight:bold}
.box{background:#f1f3f8;padding:10px;border-left:3px solid #14213d}
table{border-collapse:collapse;width:100%;font-size:13px;font-family:Arial,sans-serif}th,td{padding:4px 6px;text-align:right;border-bottom:1px solid #e3e3e3}
th:first-child,td:first-child{text-align:left}th{border-bottom:2px solid #14213d}.w{overflow-x:auto}
li{margin-bottom:5px}small{color:#666;font-family:Arial,sans-serif}
"""


def get(url, email, texto=False):
    r = requests.get(url, headers={"User-Agent": f"AnalizadorPersonal {email}"}, timeout=30)
    r.raise_for_status()
    return r.text if texto else r.json()


@st.cache_data(ttl=86400)
def lista_tickers(email):
    return get("https://www.sec.gov/files/company_tickers.json", email)


def cik_de(ticker, email):
    for d in lista_tickers(email).values():
        if d["ticker"].upper() == ticker.upper():
            return str(d["cik_str"]).zfill(10), d["title"]
    return None, None


def serie(f, tags, flujo, esc):
    out = {}
    g = f.get("facts", {}).get("us-gaap", {})
    for t in tags:
        mejores = {}
        for filas in g.get(t, {}).get("units", {}).values():
            for r in filas:
                if r.get("form") != "10-K" or r.get("fp") != "FY":
                    continue
                if flujo:
                    if "start" not in r:
                        continue
                    d = (dt.date.fromisoformat(r["end"]) - dt.date.fromisoformat(r["start"])).days
                    if not 340 <= d <= 380:
                        continue
                elif "start" in r:
                    continue
                if r["end"] not in mejores or r["filed"] > mejores[r["end"]]["filed"]:
                    mejores[r["end"]] = r
        for k, r in mejores.items():
            out.setdefault(k, r["val"] / esc)
    return out


def tabla_datos(f):
    c = {k: serie(f, t, True, e) for k, (t, e) in FLUJO.items()}
    c.update({k: serie(f, t, False, e) for k, (t, e) in SALDO.items()})
    df = pd.DataFrame(c).sort_index()
    df["Capex"] = df["Capex"].abs()
    df["FCF"] = df["CFO"] - df["Capex"]
    df["Crec"] = df["Ventas"].pct_change() * 100
    df["MargenOp"] = df["EBIT"] / df["Ventas"] * 100
    df["DeudaNeta"] = df["Deuda"] - df["Caja"]
    df["ROIC"] = df["EBIT"] * 0.79 / (df["Deuda"] + df["Patrimonio"] - df["Caja"]) * 100
    df["FCFNeto"] = df["FCF"] / df["Neto"] * 100
    df["CapexV"] = df["Capex"] / df["Ventas"] * 100
    df["EBITDA"] = df["EBIT"] + df["DA"]
    df.index = [i[:4] for i in df.index]
    return df.tail(10)


def acciones_actuales(f, df):
    filas = [r for u in f.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).values() for r in u]
    if filas:
        ult = max(r["end"] for r in filas)
        return sum({r["val"] for r in filas if r["end"] == ult}) / M
    return df["Acc"].dropna().iloc[-1]


def valor(fcf0, g, tasa, gt, dn, acc):
    vp = sum(fcf0 * (1 + g) ** t / (1 + tasa) ** t for t in range(1, 11))
    vt = fcf0 * (1 + g) ** 10 * (1 + gt) / (tasa - gt) / (1 + tasa) ** 10
    return (vp + vt - dn) / acc


def crecimiento_implicito(precio, fcf0, tasa, gt, dn, acc):
    lo, hi = -0.3, 0.6
    for _ in range(60):
        mid = (lo + hi) / 2
        if valor(fcf0, mid, tasa, gt, dn, acc) < precio:
            lo = mid
        else:
            hi = mid
    return mid


def n(v, d=1):
    return "—" if pd.isna(v) else f"{v:,.{d}f}"


def tab(cab, filas):
    h = "<div class='w'><table><tr>" + "".join(f"<th>{c}</th>" for c in cab) + "</tr>"
    for fila in filas:
        h += "<tr>" + "".join(f"<td>{c}</td>" for c in fila) + "</tr>"
    return h + "</table></div>"


def informe(nombre, ticker, df, precio, acc, tasa, gt, esc, ex):
    u = df.iloc[-1]
    fcf0, dn = u["FCF"], u["DeudaNeta"]
    if not fcf0 > 0:
        raise ValueError("La caja libre del último ejercicio no es positiva: este método de valoración no se puede aplicar.")
    gbase = dict(esc)["Base"]
    val = {k: valor(fcf0, g / 100, tasa / 100, gt / 100, dn, acc) for k, g in esc}
    graf = "<h2>Precio y valor</h2>" + grafico(ex["hist"], [(k, val[k]) for k, _ in esc], precio) if ex.get("hist") is not None else ""
    gimp = crecimiento_implicito(precio, fcf0, tasa / 100, gt / 100, dn, acc) * 100
    pot = (val["Base"] / precio - 1) * 100
    cons = ""
    if ex.get("cons"):
        tc, na = ex["cons"]
        cons = ("<h2>Dónde discrepamos del consenso</h2>" + tab(["", "Valor", "vs precio"],
                [[f"Consenso ({na} analistas)", f"{tc:,.2f} $", f"{(tc/precio-1)*100:+.1f} %"], ["Nuestro escenario base", f"{val['Base']:,.2f} $", f"{pot:+.1f} %"]])
                + "<small>El consenso es un dato de mercado, no información oficial de la empresa.</small>")
    mcap = precio * acc
    ev = mcap + dn
    ult = df.index[-1]
    riesgos = []
    if u["EBITDA"] > 0 and dn / u["EBITDA"] > 3:
        riesgos.append(f"Deuda neta de {n(dn,0)} M$, {n(dn/u['EBITDA'])} veces el EBITDA.")
    if df["MargenOp"].tail(3).is_monotonic_decreasing and df["MargenOp"].tail(3).nunique() > 1:
        riesgos.append("El margen operativo ha bajado en los dos últimos ejercicios.")
    if u["Crec"] < 3:
        riesgos.append(f"El crecimiento de ventas se ha frenado al {n(u['Crec'])} %.")
    if u["FCFNeto"] < 80:
        riesgos.append(f"La caja libre solo cubre el {n(u['FCFNeto'],0)} % del beneficio neto.")
    if gimp > gbase:
        riesgos.append(f"El precio exige {n(gimp)} % anual de crecimiento, más que el {n(gbase)} % del escenario base.")
    riesgos = riesgos or ["Sin alertas automáticas con los criterios actuales."]

    cuentas = tab(["Ejercicio", "Ventas", "Crec. %", "Margen op. %", "Beneficio neto", "Caja libre"],
                  [[i, n(r.Ventas, 0), n(r.Crec), n(r.MargenOp), n(r.Neto, 0), n(r.FCF, 0)] for i, r in df.iloc[::-1].iterrows()])
    balance = tab(["Ejercicio", "Caja", "Deuda", "Deuda neta", "Patrimonio"],
                  [[i, n(r.Caja, 0), n(r.Deuda, 0), n(r.DeudaNeta, 0), n(r.Patrimonio, 0)] for i, r in df.tail(4).iloc[::-1].iterrows()])
    t6 = df.tail(6)
    calidad = tab(["Indicador"] + list(t6.index),
                  [["ROIC %"] + [n(v) for v in t6["ROIC"]], ["Margen operativo %"] + [n(v) for v in t6["MargenOp"]],
                   ["Caja libre / beneficio neto %"] + [n(v) for v in t6["FCFNeto"]], ["Capex / ventas %"] + [n(v) for v in t6["CapexV"]]])
    a, b = t6.iloc[0], t6.iloc[-1]
    asig = tab(["Concepto", f"{t6.index[0]}", f"{t6.index[-1]}"],
               [["Acciones diluidas (M)", n(a.Acc), n(b.Acc)], ["Dividendo por acción ($)", n(a.Div, 2), n(b.Div, 2)], ["Deuda neta (M$)", n(a.DeudaNeta, 0), n(b.DeudaNeta, 0)]])
    escs = tab(["Escenario", "Crec. anual", "Valor/acción", "vs precio"],
               [[k, f"{n(g)} %", f"{val[k]:,.2f} $", f"{(val[k]/precio-1)*100:+.1f} %"] for k, g in esc])
    mercado = tab(["Dato", "Valor"], [
        ["Capitalización (M$)", n(mcap, 0)], ["Valor de empresa (M$)", n(ev, 0)], ["PER (último ejercicio)", n(precio / u["EPS"])],
        ["EV / beneficio operativo", n(ev / u["EBIT"])], ["Rentabilidad por caja libre %", n(fcf0 / mcap * 100)], ["Deuda neta (M$)", n(dn, 0)]])
    return f"""<html><head><meta name='viewport' content='width=device-width,initial-scale=1'><style>{CSS}</style></head><body>
<h1>{nombre} <small>{ticker.upper()}</small></h1>
<div class='t'>A {precio:,.2f} $ el precio descuenta un crecimiento de la caja libre del {n(gimp)} % anual, frente al {n(gbase)} % de nuestro escenario base: valor de {val['Base']:,.2f} $ ({pot:+.1f} %).</div>
{ex['tesis']}<div class='g'><div>
<h2>Qué descuenta el precio</h2><div class='box'>El precio exige que la caja libre de partida ({n(fcf0,0)} M$) crezca un {n(gimp)} % anual durante diez años (tasa de descuento {n(tasa)} %, crecimiento a perpetuidad {n(gt)} %). Las ventas crecieron un {n(u['Crec'])} % en {ult}.</div>
{graf}<h2>Alertas</h2><ul>{''.join(f'<li>{r}</li>' for r in riesgos)}</ul>
{ex['riesgos']}<h2>10 años de cuentas (M$)</h2>{cuentas}
<h2>Balance al cierre de cada año (M$)</h2>{balance}{ex['trim']}
</div><div>
<div class='card'><small style='color:#ccd'>VALOR POR ACCIÓN · ESCENARIO BASE</small><div class='v'>{val['Base']:,.2f} $</div>{pot:+.1f} % frente a {precio:,.2f} $<div style='margin-top:8px;font-size:13px'>{ex['lectura']}</div></div>
<h2>Escenarios</h2>{escs}{cons}{ex['s30']}
<h2>Datos de mercado</h2>{mercado}
<h2>Calidad del negocio</h2>{calidad}<small>ROIC: beneficio operativo tras impuesto teórico del 21 % entre deuda + patrimonio − caja.</small>{ex['a_cal']}
<h2>Asignación de capital</h2>{asig}{ex['a_cap']}{ex['rivales']}{ex['insiders']}{ex['k8']}
</div></div>
{ex['direccion']}
<p><small>Fuentes: cuentas oficiales presentadas a la SEC (formularios 10-K). El precio de cotización procede de fuentes de mercado en internet, no de la empresa. Valoración: descuento de flujos de caja libre. No es una recomendación de inversión.</small></p></body></html>"""


ITEMS = {"2.02": "Resultados", "5.02": "Cambios en directivos o consejo", "1.01": "Acuerdo relevante", "7.01": "Información (Reg FD)",
         "8.01": "Otros hechos relevantes", "5.07": "Votación de accionistas", "2.03": "Nueva deuda", "1.02": "Fin de acuerdo"}


def precio_internet(ticker, apikey=""):
    """Precio y cierres de 3 años desde internet (Yahoo Finance; Stooq si hay clave). Devuelve (None, None) si falla."""
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="3y")["Close"].dropna()
        if len(h) > 20:
            return float(h.iloc[-1]), h
    except Exception:
        pass
    if apikey:
        try:
            r = requests.get("https://stooq.com/q/d/l/", params={"s": f"{ticker.lower()}.us", "i": "d", "apikey": apikey}, timeout=20)
            d = pd.read_csv(io.StringIO(r.text), parse_dates=["Date"]).set_index("Date")["Close"].dropna()
            d = d[d.index >= d.index[-1] - pd.Timedelta(days=1095)]
            if len(d) > 20:
                return float(d.iloc[-1]), d
        except Exception:
            pass
    return None, None


def grafico(hist, esc, precio):
    w, h, p = 700, 260, 30
    ys = [float(v) for v in hist.values[:: max(1, len(hist) // 300)]]
    lv = [v for _, v in esc]
    lo, hi = min(ys + lv) * 0.95, max(ys + lv) * 1.05
    X = lambda i: p + i * (w - 2 * p) / (len(ys) - 1)
    Y = lambda v: h - p - (v - lo) / (hi - lo) * (h - 2 * p)
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(ys))
    col = {"Optimista": "#2e7d32", "Base": "#b8860b", "Pesimista": "#b71c1c"}
    ln = "".join(f"<line x1='{p}' x2='{w-p}' y1='{Y(v):.1f}' y2='{Y(v):.1f}' stroke='{col[k]}' stroke-dasharray='5 4'/>"
                 f"<text x='{w-p}' y='{Y(v)-3:.1f}' font-size='11' text-anchor='end' fill='{col[k]}'>{k} {v:,.0f} $</text>" for k, v in esc)
    return (f"<svg viewBox='0 0 {w} {h}' width='100%'><polyline points='{pts}' fill='none' stroke='#14213d' stroke-width='1.5'/>{ln}"
            f"<text x='{p}' y='14' font-size='11'>Cierre {precio:,.2f} $ · últimos 3 años (precio de mercado)</text></svg>")


def trim_df(f):
    g = f.get("facts", {}).get("us-gaap", {})
    res = {}
    for k in ["Ventas", "EBIT", "Neto", "CFO", "Capex"]:
        ytd = {}
        for t in FLUJO[k][0]:
            for r in g.get(t, {}).get("units", {}).get("USD", []):
                if "start" not in r or r.get("form") not in ("10-Q", "10-K"):
                    continue
                d = (dt.date.fromisoformat(r["end"]) - dt.date.fromisoformat(r["start"])).days
                b = 1 if 80 <= d <= 100 else 2 if 170 <= d <= 195 else 3 if 260 <= d <= 285 else 4 if 340 <= d <= 380 else 0
                if b:
                    ytd.setdefault((r["start"], b), (r["end"], r["val"] / M))
        q = {}
        for (s, b), (e, v) in ytd.items():
            if b == 1:
                q.setdefault(e, v)
            elif (s, b - 1) in ytd:
                q.setdefault(e, v - ytd[(s, b - 1)][1])
        res[k] = q
    d = pd.DataFrame(res).sort_index().dropna(subset=["Ventas"])
    d["FCF"] = d["CFO"] - d["Capex"].abs()
    return d


def html_trim(f):
    d = trim_df(f)
    filas = []
    for e, r in d.tail(4).iloc[::-1].iterrows():
        prev = [x for x in d.index if abs((dt.date.fromisoformat(e) - dt.date.fromisoformat(x)).days - 365) <= 12]
        cr = (r.Ventas / d.loc[prev[0], "Ventas"] - 1) * 100 if prev else float("nan")
        filas.append([e, n(r.Ventas, 0), n(cr), n(r.EBIT / r.Ventas * 100), n(r.Neto, 0), n(r.FCF, 0)])
    return tab(["Cierre", "Ventas", "Crec. interanual %", "Margen op. %", "Beneficio neto", "Caja libre"], filas)


def ficha_df(nombre, df, acc, precio):
    u = df.iloc[-1]
    nan = float("nan")
    return {"n": nombre, "cap": precio * acc if precio else nan, "per": precio / u["EPS"] if precio else nan, "cr": u["Crec"],
            "mo": u["MargenOp"], "fv": u["FCF"] / u["Ventas"] * 100, "de": u["DeudaNeta"] / u["EBITDA"]}


def ficha_rival(tk, email, apikey):
    cik, nom = cik_de(tk, email)
    f = get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", email)
    df = tabla_datos(f)
    return ficha_df(nom, df, acciones_actuales(f, df), precio_internet(tk, apikey)[0])


def html_rivales(fs):
    c = ["Empresa", "Capit. M$", "PER", "Crec. ventas %", "Margen op. %", "FCF/ventas %", "Deuda neta/EBITDA"]
    fila = lambda x: [x["n"][:22], n(x["cap"], 0), n(x["per"]), n(x["cr"]), n(x["mo"]), n(x["fv"]), n(x["de"])]
    filas = [fila(x) for x in fs]
    m = lambda k: pd.Series([x[k] for x in fs[1:]]).median()
    filas.append(["Mediana rivales", n(m("cap"), 0), n(m("per")), n(m("cr")), n(m("mo")), n(m("fv")), n(m("de"))])
    return "<h2>Contra sus rivales</h2>" + tab(c, filas) + "<small>Último ejercicio de cada una (10-K). Capitalización y PER usan precio de mercado.</small>"


def html_insiders(sub, cik, email):
    rec = sub["filings"]["recent"]
    hoy, tx, cnt = dt.date.today(), [], 0
    for form, fecha, acc, doc in zip(rec["form"], rec["filingDate"], rec["accessionNumber"], rec["primaryDocument"]):
        if form != "4":
            continue
        if (hoy - dt.date.fromisoformat(fecha)).days > 365 or cnt >= 30:
            break
        cnt += 1
        try:
            x = get(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc.split('/')[-1]}", email, True)
            root = ET.fromstring(x)
        except Exception:
            continue
        time.sleep(0.12)
        nom = root.findtext(".//rptOwnerName") or "—"
        cargo = root.findtext(".//officerTitle") or ("Consejero" if root.findtext(".//isDirector") in ("1", "true") else "")
        plan = "10b5-1" in x
        for t in root.findall(".//nonDerivativeTransaction"):
            code = t.findtext(".//transactionCode")
            if code in ("P", "S"):
                sh = float(t.findtext(".//transactionShares/value") or 0)
                px = float(t.findtext(".//transactionPricePerShare/value") or 0)
                tx.append((t.findtext(".//transactionDate/value") or fecha, nom, cargo, code, sh, sh * px, plan))
    comp, vent = [t for t in tx if t[3] == "P"], [t for t in tx if t[3] == "S"]
    tv = sum(t[5] for t in vent)
    pl = sum(t[5] for t in vent if t[6]) / tv * 100 if tv else 0
    res = (f"<p>Compras: {len(comp)} · ${sum(t[5] for t in comp)/1e6:,.1f} M. Ventas: {len(vent)} · ${tv/1e6:,.1f} M, "
           f"de ellas {pl:.0f} % con plan 10b5-1 (programadas de antemano).</p>")
    filas = [[t[0], f"{t[1]} ({t[2]})", "Compra" if t[3] == "P" else "Venta", f"{t[4]:,.0f}", f"${t[5]:,.0f}", "sí" if t[6] else "no"] for t in tx[:8]]
    return "<h2>Directivos (formularios 4, 12 meses)</h2>" + res + (tab(["Fecha", "Directivo", "Tipo", "Acciones", "Importe", "Plan 10b5-1"], filas) if filas else "")


def html_8k(sub, cik):
    rec = sub["filings"]["recent"]
    filas = []
    for form, fecha, acc, doc, it in zip(rec["form"], rec["filingDate"], rec["accessionNumber"], rec["primaryDocument"], rec["items"]):
        if form == "8-K":
            temas = ", ".join(ITEMS.get(i, i) for i in it.split(",") if i and i != "9.01") or "—"
            filas.append([fecha, temas, f"<a href='https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}' target='_blank'>ver</a>"])
            if len(filas) == 6:
                break
    return "<h2>Lo contado a la SEC (8-K)</h2>" + tab(["Fecha", "Temas", "Documento"], filas)


def seguro(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        return f"<p><small>No disponible: {e}</small></p>"


def limpiar(x):
    x = re.sub(r"(?is)<(script|style).*?</\1>", " ", x)
    return re.sub(r"\s+", " ", hmod.unescape(re.sub(r"<[^>]+>", " ", x))).strip()


def norm(s):
    return re.sub(r"[\s“”\"'’‘]+", " ", s).lower().strip()


def texto_8k(cik, acc, doc, email, maxc):
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/"
    arch = doc
    try:
        nombres = [i["name"] for i in get(base + "index.json", email)["directory"]["item"]]
        ex = [x for x in nombres if re.search(r"(?i)ex[-_]?99", x) and x.lower().endswith((".htm", ".html"))]
        arch = ex[0] if ex else doc
    except Exception:
        pass
    return limpiar(get(base + arch, email, True))[:maxc]


def comunicados(sub, cik, email):
    rec = sub["filings"]["recent"]
    res, otros = [], []
    for form, fecha, acc, doc, it in zip(rec["form"], rec["filingDate"], rec["accessionNumber"], rec["primaryDocument"], rec["items"]):
        if form != "8-K":
            continue
        try:
            if "2.02" in it.split(","):
                if len(res) < 2:
                    res.append((f"comunicado de resultados {fecha}", texto_8k(cik, acc, doc, email, 14000)))
            elif len(otros) < 2:
                otros.append((f"8-K {fecha} (temas {it})", texto_8k(cik, acc, doc, email, 2500)))
        except Exception:
            pass
        time.sleep(0.12)
        if len(res) == 2 and len(otros) == 2:
            break
    return res + otros


def consenso_mercado(ticker):
    try:
        import yfinance as yf
        i = yf.Ticker(ticker).info
        if i.get("targetMeanPrice"):
            return float(i["targetMeanPrice"]), i.get("numberOfAnalystOpinions") or "—"
    except Exception:
        pass
    return None


def bloques(r, citas, nota):
    e = hmod.escape
    claves = ["tesis", "lectura", "riesgos", "s30", "a_cal", "a_cap", "a_dir", "direccion"]
    if r is None:
        return {**{k: "" for k in claves}, "direccion": f"<h2>Lo que dijo la dirección</h2><p><small>{e(nota)}</small></p>"}
    ul = lambda xs: "<ul>" + "".join(f"<li>{e(str(x))}</li>" for x in xs) + "</ul>"
    an = lambda k: f"<p><small><b>ANÁLISIS</b> {e(r.get(k) or '')}</small></p>" if r.get(k) else ""
    d = "<h2>Lo que dijo la dirección (comunicados presentados a la SEC)</h2>" + an("a_8k")
    for c in citas:
        d += (f"<blockquote style='margin:8px 0;padding-left:10px;border-left:3px solid #b8860b'><i>“{e(c['original'])}”</i><br>"
              f"{e(c.get('traduccion', ''))}{'<br>' if c.get('traduccion') else ''}<small>{e(c.get('quien', ''))} · {e(str(c.get('fecha', '')))}</small></blockquote>")
    if r.get("cambios"):
        d += "<h2>Qué cambió frente al comunicado anterior</h2>" + tab(["Tema", "Antes", "Ahora"], [[e(c.get("tema", "")), e(c.get("antes", "")), e(c.get("ahora", ""))] for c in r["cambios"]])
    if r.get("esencial"):
        d += "<h2>Lo esencial del último comunicado</h2>" + ul(r["esencial"])
    return {"tesis": ul(r.get("tesis", [])), "lectura": "<b>NUESTRA LECTURA</b> · " + e(r.get("lectura", "")),
            "riesgos": "<h2>Riesgos clave</h2>" + ul(r.get("riesgos", [])), "s30": "<h2>El análisis en 30 segundos</h2><p>" + e(r.get("resumen30", "")) + "</p>",
            "a_cal": an("a_calidad"), "a_cap": an("a_capital"), "a_dir": an("a_directivos"), "direccion": d}


KW = re.compile(r"(?i)revenue|income from operations|operating income|earnings per share|diluted|same[- ]store|comparable|net new stores|store openings|outlook|guidance|expect|free cash flow|cash flow")
TEMAS = [("Ventas comparables", r"same[- ]store|comparable"), ("Aperturas de tiendas", r"net new stores|store openings|net store"), ("Perspectivas", r"outlook|guidance|we expect|we now expect")]


def frases(texto, n=3):
    fs = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z“\"])", texto) if 50 <= len(s) <= 320 and not re.search(r"(?i)forward-looking|safe harbor|www\.|©|undue reliance|[“\"]", s)]
    pts = sorted(((len(KW.findall(s)) + 2 * bool(re.search(r"\d(\.\d+)?\s?%|\$\s?\d", s)), -i) for i, s in enumerate(fs)), reverse=True)[:n]
    return [fs[-i] for _, i in sorted(pts, key=lambda x: -x[1])]


def citas_de(texto, fecha):
    out = []
    for m in re.finditer(r"[“\"]([^”\"]{60,500})[”\"],?\s+(?:said|commented|stated|added|noted)\s+([A-Z][^.“\"]{3,90})", texto):
        out.append({"original": m.group(1).strip(" ,"), "traduccion": "", "quien": m.group(2).strip(" ,"), "fecha": fecha})
    for m in re.finditer(r"([A-Z][^.“\"]{3,90}?),?\s+(?:said|commented|stated|added|noted):?\s*[“\"]([^”\"]{60,500})[”\"]", texto):
        out.append({"original": m.group(2).strip(), "traduccion": "", "quien": m.group(1).strip(" ,"), "fecha": fecha})
    return out[:3]


def cambios_trim(d):
    if d is None or len(d) < 2:
        return []
    def cr(e):
        p = [x for x in d.index if abs((dt.date.fromisoformat(e) - dt.date.fromisoformat(x)).days - 365) <= 12]
        return (d.loc[e, "Ventas"] / d.loc[p[0], "Ventas"] - 1) * 100 if p else float("nan")
    a, b = d.index[-2], d.index[-1]
    mo = lambda e: d.loc[e, "EBIT"] / d.loc[e, "Ventas"] * 100
    return [{"tema": "Crecimiento interanual de ventas", "antes": f"{n(cr(a))} % ({a})", "ahora": f"{n(cr(b))} % ({b})"},
            {"tema": "Margen operativo", "antes": f"{n(mo(a))} % ({a})", "ahora": f"{n(mo(b))} % ({b})"}]


def redactar(nombre, precio, esc, vals, gimp, tasa, df, ins_txt, docs, trim, transcripcion=""):
    u, a1 = df.iloc[-1], df.index[-1]
    k0 = max(len(df) - 6, 0)
    g0, a0 = df.iloc[k0], df.index[k0]
    gb, go = dict(esc)["Base"], dict(esc)["Optimista"]
    vb, vo = vals["Base"], vals["Optimista"]
    pot = (vb / precio - 1) * 100
    t2 = (f"Ni siquiera el escenario optimista ({n(go)} %, {vo:,.2f} $) llega al precio actual: {(vo/precio-1)*100:+.1f} %." if vo < precio
          else f"El escenario optimista ({n(go)} %, {vo:,.2f} $) supera el precio actual en un {(vo/precio-1)*100:.1f} %.")
    tesis = [f"A {precio:,.2f} $ el precio descuenta un crecimiento anual de la caja libre del {n(gimp)} %, {gimp-gb:+.1f} puntos sobre el escenario base ({n(gb)} %), que vale {vb:,.2f} $: potencial del {pot:+.1f} %.",
             t2,
             f"El ROIC de {a1} es del {n(u.ROIC)} % y el margen operativo del {n(u.MargenOp)} % ({u.MargenOp-g0.MargenOp:+.1f} puntos desde {a0}).",
             f"Las ventas crecieron un {n(u.Crec)} % en {a1}; la deuda neta es de {n(u.DeudaNeta, 0)} M$ ({n(u.DeudaNeta/u.EBITDA)} veces el EBITDA)."]
    lectura = ("Cara: el precio paga más que nuestro escenario base." if pot < -10 else
               "Barata: el precio paga menos que nuestro escenario base." if pot > 10 else "Razonable: el precio está cerca de nuestro escenario base.")
    rs = []
    if u.EBITDA > 0 and u.DeudaNeta / u.EBITDA > 3:
        rs.append(f"Deuda neta de {n(u.DeudaNeta, 0)} M$, {n(u.DeudaNeta/u.EBITDA)} veces el EBITDA.")
    if df["MargenOp"].tail(3).is_monotonic_decreasing and df["MargenOp"].tail(3).nunique() > 1:
        rs.append("El margen operativo ha bajado en los dos últimos ejercicios.")
    if u.Crec < 3:
        rs.append(f"El crecimiento de ventas se ha frenado al {n(u.Crec)} %.")
    if u.FCFNeto < 80:
        rs.append(f"La caja libre solo cubre el {n(u.FCFNeto, 0)} % del beneficio neto.")
    if gimp > gb:
        rs.append(f"El precio exige un {n(gimp)} % anual de crecimiento, más que el {n(gb)} % del escenario base.")
    m = re.search(r"Compras: (\d+) · \$([\d,.]+) M\. Ventas: (\d+) · \$([\d,.]+) M, de ellas (\d+) %", ins_txt)
    a_dir = (f"En los últimos 12 meses: {m.group(1)} compras (${m.group(2)} M) y {m.group(3)} ventas (${m.group(4)} M); el {m.group(5)} % de lo vendido, con plan 10b5-1."
             + (" No hay compras de directivos en el mercado." if m.group(1) == "0" else "")) if m else ""
    com = [d for d in docs if d[0].startswith("comunicado")]
    fecha = com[0][0].split()[-1] if com else ""
    esencial, citas, cambios = [], [], cambios_trim(trim)
    if com:
        esencial = frases(com[0][1])
        citas = citas_de(com[0][1], fecha)
        if len(com) > 1:
            vistos = set()
            for tema, pat in TEMAS:
                f = [next((s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z“\"])", c[1]) if re.search(pat, s, re.I) and 40 <= len(s) <= 300), None) for c in com[:2]]
                if f[0] and f[1] and (f[0], f[1]) not in vistos:
                    vistos.add((f[0], f[1]))
                    cambios.append({"tema": tema, "antes": f[1], "ahora": f[0]})
    if transcripcion.strip():
        for s in re.split(r"(?<=[.!?])\s+", transcripcion):
            if re.search(r"(?i)\bwe (now )?(expect|continue|remain|believe)\b", s) and 60 <= len(s) <= 350 and len(citas) < 6:
                citas.append({"original": s.strip(), "traduccion": "", "quien": "Dirección (transcripción oficial)", "fecha": ""})
    return {"tesis": tesis, "lectura": lectura,
            "resumen30": (f"Con ventas de {n(u.Ventas, 0)} M$ y un margen operativo del {n(u.MargenOp)} %, {nombre} genera {n(u.FCF, 0)} M$ de caja libre. "
                          f"A {precio:,.2f} $ el mercado descuenta un {n(gimp)} % anual de crecimiento de esa caja, frente al {n(gb)} % del escenario base: valor de {vb:,.2f} $ ({pot:+.1f} %)."),
            "riesgos": (rs or ["Sin alertas automáticas con los criterios actuales."])[:3],
            "a_calidad": (f"El ROIC del {n(u.ROIC)} % {'supera' if u.ROIC > tasa else 'no alcanza'} la tasa de descuento del {n(tasa)} %; el margen operativo pasa del {n(g0.MargenOp)} % al {n(u.MargenOp)} % desde {a0} "
                          f"y la caja libre equivale al {n(u.FCFNeto, 0)} % del beneficio neto."),
            "a_capital": (f"Desde {a0} las acciones diluidas pasan de {n(g0.Acc)} M a {n(u.Acc)} M, el dividendo por acción de {n(g0.Div, 2)} $ a {n(u.Div, 2)} $ y la deuda neta de {n(g0.DeudaNeta, 0)} M$ a {n(u.DeudaNeta, 0)} M$. "
                          f"La caja libre de los últimos 5 ejercicios suma {n(df['FCF'].tail(5).sum(), 0)} M$."),
            "a_directivos": a_dir,
            "a_8k": (f"Último comunicado de resultados presentado a la SEC: {fecha}. Las frases de abajo son extractos literales de los documentos oficiales, en su idioma original." if com else
                     "No se han podido leer los comunicados de resultados de la SEC."),
            "esencial": esencial, "cambios": cambios}, citas


def init():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS informes (ticker TEXT, nombre TEXT, fecha TEXT, html TEXT)")
    return con


st.set_page_config(page_title="Informe de valoración", layout="wide")
st.title("Informe de valoración")
email = st.sidebar.text_input("Tu email (la SEC lo exige para identificarte)")
stooq = st.sidebar.text_input("Clave de Stooq (opcional, solo si falla el precio)", type="password")
tasa = st.sidebar.slider("Tasa de descuento %", 6.0, 15.0, 10.0, 0.5)
gt = st.sidebar.slider("Crecimiento a perpetuidad %", 0.0, 4.0, 2.0, 0.5)
usa_cons = st.sidebar.checkbox("Incluir consenso de analistas (dato de mercado, no oficial)", value=True)
ticker = st.text_input("Ticker (por ejemplo, DPZ)").strip()
rivales = st.text_input("Rivales (tickers separados por comas, opcional)")
st.write("Crecimiento anual de la caja libre durante 10 años (%):")
c1, c2, c3 = st.columns(3)
g_mal = c1.number_input("Malo", value=3.0, step=0.5)
g_base = c2.number_input("Base", value=5.0, step=0.5)
g_opt = c3.number_input("Optimista", value=7.0, step=0.5)
manual = st.number_input("Precio manual en $ (0 = buscarlo en internet)", min_value=0.0, value=0.0, step=0.5)
transc = st.text_area("Transcripción oficial de la conferencia (opcional; pégala si la publica la empresa)")

if st.button("Generar informe") and ticker:
    if "@" not in email:
        st.error("Escribe tu email en la barra lateral.")
    else:
        try:
            cik, nombre = cik_de(ticker, email)
            if not cik:
                st.error("No encuentro ese ticker en la SEC.")
                st.stop()
            f = get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", email)
            df = tabla_datos(f)
            acc = acciones_actuales(f, df)
            px, hist = precio_internet(ticker, stooq)
            precio = manual or px
            if not precio:
                st.error("No he podido obtener el precio de internet. Escríbelo a mano en la casilla de precio.")
                st.stop()
            sub = get(f"https://data.sec.gov/submissions/CIK{cik}.json", email)
            fs = [ficha_df(nombre, df, acc, precio)]
            for tk in [t.strip() for t in rivales.split(",") if t.strip()]:
                try:
                    fs.append(ficha_rival(tk, email, stooq))
                except Exception:
                    pass
            esc = [("Optimista", g_opt), ("Base", g_base), ("Pesimista", g_mal)]
            ins = seguro(html_insiders, sub, cik, email)
            u = df.iloc[-1]
            u = df.iloc[-1]
            r, citas, nota = None, [], "No se pudieron redactar los textos."
            if u["FCF"] > 0:
                try:
                    vals = {k: valor(u["FCF"], g / 100, tasa / 100, gt / 100, u["DeudaNeta"], acc) for k, g in esc}
                    gimp = crecimiento_implicito(precio, u["FCF"], tasa / 100, gt / 100, u["DeudaNeta"], acc) * 100
                    try:
                        docs = comunicados(sub, cik, email)
                    except Exception:
                        docs = []
                    try:
                        trim = trim_df(f)
                    except Exception:
                        trim = None
                    r, citas = redactar(nombre, precio, esc, vals, gimp, tasa, df, limpiar(ins), docs, trim, transc)
                except Exception as e:
                    r, nota = None, f"No se pudieron redactar los textos: {e}"
            b = bloques(r, citas, nota)
            ex = {"hist": hist, "trim": "<h2>Últimos 4 trimestres (M$)</h2>" + seguro(html_trim, f),
                  "rivales": seguro(html_rivales, fs) if len(fs) > 1 else "", "insiders": ins + b["a_dir"], "k8": seguro(html_8k, sub, cik),
                  "cons": consenso_mercado(ticker) if usa_cons else None, **{k: b[k] for k in ["tesis", "lectura", "riesgos", "s30", "a_cal", "a_cap", "direccion"]}}
            informe_html = informe(nombre, ticker, df, precio, acc, tasa, gt, esc, ex)
            con = init()
            con.execute("INSERT INTO informes VALUES (?,?,?,?)", (ticker.upper(), nombre, dt.datetime.now().isoformat(timespec="seconds"), informe_html))
            con.commit()
            st.download_button("Descargar informe (HTML)", informe_html, file_name=f"{ticker.upper()}.html", mime="text/html")
            components.html(informe_html, height=5200, scrolling=True)
        except Exception as e:
            st.error(f"Ha habido un error: {e}")

st.divider()
st.subheader("Historial")
filas = init().execute("SELECT ticker, nombre, fecha, html FROM informes ORDER BY fecha DESC").fetchall()
if filas:
    i = st.selectbox("Informes guardados", range(len(filas)), format_func=lambda k: f"{filas[k][0]} · {filas[k][1]} · {filas[k][2]}")
    components.html(filas[i][3], height=5200, scrolling=True)
