import io
import sqlite3
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


def get(url, email):
    r = requests.get(url, headers={"User-Agent": f"AnalizadorPersonal {email}"}, timeout=30)
    r.raise_for_status()
    return r.json()


def cik_de(ticker, email):
    for d in get("https://www.sec.gov/files/company_tickers.json", email).values():
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


def informe(nombre, ticker, df, precio, acc, tasa, gt, gbase):
    u = df.iloc[-1]
    fcf0, dn = u["FCF"], u["DeudaNeta"]
    if not fcf0 > 0:
        raise ValueError("La caja libre del último ejercicio no es positiva: este método de valoración no se puede aplicar.")
    esc = [("Optimista", gbase + 2), ("Base", gbase), ("Pesimista", max(gbase - 2, 0))]
    val = {k: valor(fcf0, g / 100, tasa / 100, gt / 100, dn, acc) for k, g in esc}
    gimp = crecimiento_implicito(precio, fcf0, tasa / 100, gt / 100, dn, acc) * 100
    pot = (val["Base"] / precio - 1) * 100
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
<div class='g'><div>
<h2>Qué descuenta el precio</h2><div class='box'>El precio exige que la caja libre de partida ({n(fcf0,0)} M$) crezca un {n(gimp)} % anual durante diez años (tasa de descuento {n(tasa)} %, crecimiento a perpetuidad {n(gt)} %). Las ventas crecieron un {n(u['Crec'])} % en {ult}.</div>
<h2>Alertas</h2><ul>{''.join(f'<li>{r}</li>' for r in riesgos)}</ul>
<h2>10 años de cuentas (M$)</h2>{cuentas}
<h2>Balance al cierre de cada año (M$)</h2>{balance}
</div><div>
<div class='card'><small style='color:#ccd'>VALOR POR ACCIÓN · ESCENARIO BASE</small><div class='v'>{val['Base']:,.2f} $</div>{pot:+.1f} % frente a {precio:,.2f} $</div>
<h2>Escenarios</h2>{escs}
<h2>Datos de mercado</h2>{mercado}
<h2>Calidad del negocio</h2>{calidad}<small>ROIC: beneficio operativo tras impuesto teórico del 21 % entre deuda + patrimonio − caja.</small>
<h2>Asignación de capital</h2>{asig}
</div></div>
<p><small>Fuentes: cuentas oficiales presentadas a la SEC (formularios 10-K). El precio lo introduce el usuario. Valoración: descuento de flujos de caja libre. No es una recomendación de inversión.</small></p></body></html>"""


def init():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS informes (ticker TEXT, nombre TEXT, fecha TEXT, html TEXT)")
    return con


st.set_page_config(page_title="Informe de valoración", layout="wide")
st.title("Informe de valoración")
email = st.sidebar.text_input("Tu email (la SEC lo exige para identificarte)")
tasa = st.sidebar.slider("Tasa de descuento %", 6.0, 15.0, 10.0, 0.5)
gt = st.sidebar.slider("Crecimiento a perpetuidad %", 0.0, 4.0, 2.0, 0.5)
gbase = st.sidebar.slider("Crecimiento anual de la caja libre, escenario base %", 0.0, 15.0, 5.0, 0.5)
ticker = st.text_input("Ticker (por ejemplo, DPZ)").strip()
precio = st.number_input("Precio actual de la acción ($)", min_value=0.0, value=0.0, step=0.5)

if st.button("Generar informe") and ticker and precio > 0:
    if "@" not in email:
        st.error("Escribe tu email en la barra lateral.")
    else:
        try:
            cik, nombre = cik_de(ticker, email)
            if not cik:
                st.error("No encuentro ese ticker en la SEC.")
            else:
                f = get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", email)
                df = tabla_datos(f)
                html = informe(nombre, ticker, df, precio, acciones_actuales(f, df), tasa, gt, gbase)
                con = init()
                con.execute("INSERT INTO informes VALUES (?,?,?,?)", (ticker.upper(), nombre, dt.datetime.now().isoformat(timespec="seconds"), html))
                con.commit()
                st.download_button("Descargar informe (HTML)", html, file_name=f"{ticker.upper()}.html", mime="text/html")
                components.html(html, height=3200, scrolling=True)
        except Exception as e:
            st.error(f"Ha habido un error: {e}")

st.divider()
st.subheader("Historial")
filas = init().execute("SELECT ticker, nombre, fecha, html FROM informes ORDER BY fecha DESC").fetchall()
if filas:
    i = st.selectbox("Informes guardados", range(len(filas)), format_func=lambda k: f"{filas[k][0]} · {filas[k][1]} · {filas[k][2]}")
    components.html(filas[i][3], height=3200, scrolling=True)
