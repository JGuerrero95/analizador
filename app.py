import io
import sqlite3
import datetime as dt

import pandas as pd
import requests
import streamlit as st

DB = "historial.db"

# Conceptos contables oficiales (XBRL us-gaap) que las empresas presentan a la SEC
CONCEPTOS = {
    "Ventas": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "Beneficio operativo": ["OperatingIncomeLoss"],
    "Beneficio neto": ["NetIncomeLoss"],
    "Flujo de operaciones": ["NetCashProvidedByUsedInOperatingActivities"],
    "Capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
}


def sec_get(url, email):
    r = requests.get(url, headers={"User-Agent": f"AnalizadorPersonal {email}"}, timeout=30)
    r.raise_for_status()
    return r.json()


def buscar_cik(ticker, email):
    datos = sec_get("https://www.sec.gov/files/company_tickers.json", email)
    for d in datos.values():
        if d["ticker"].upper() == ticker.upper():
            return str(d["cik_str"]).zfill(10), d["title"]
    return None, None


def serie_anual(facts, etiquetas):
    """Valores anuales (10-K, ejercicio completo) en millones de USD, por fecha de cierre."""
    out = {}
    gaap = facts.get("facts", {}).get("us-gaap", {})
    for etiqueta in etiquetas:
        filas = gaap.get(etiqueta, {}).get("units", {}).get("USD", [])
        mejores = {}
        for f in filas:
            if f.get("form") != "10-K" or f.get("fp") != "FY" or "start" not in f:
                continue
            dias = (dt.date.fromisoformat(f["end"]) - dt.date.fromisoformat(f["start"])).days
            if not 340 <= dias <= 380:
                continue
            if f["end"] not in mejores or f["filed"] > mejores[f["end"]]["filed"]:
                mejores[f["end"]] = f
        for fin, f in mejores.items():
            out.setdefault(fin, f["val"] / 1e6)
    return out


def construir_tabla(facts):
    cols = {nombre: serie_anual(facts, tags) for nombre, tags in CONCEPTOS.items()}
    df = pd.DataFrame(cols).sort_index().tail(10)
    df["Capex"] = df["Capex"].abs()
    df["Caja libre"] = df["Flujo de operaciones"] - df["Capex"]
    df["Crecimiento ventas %"] = df["Ventas"].pct_change() * 100
    df["Margen operativo %"] = df["Beneficio operativo"] / df["Ventas"] * 100
    df["Caja libre / ventas %"] = df["Caja libre"] / df["Ventas"] * 100
    df.index = [i[:4] for i in df.index]
    return df.round(1)


def guardar(ticker, nombre, df):
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS analisis (ticker TEXT, nombre TEXT, fecha TEXT, datos TEXT)")
    con.execute(
        "INSERT INTO analisis VALUES (?,?,?,?)",
        (ticker, nombre, dt.datetime.now().isoformat(timespec="seconds"), df.to_json()),
    )
    con.commit()
    con.close()


def historial():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS analisis (ticker TEXT, nombre TEXT, fecha TEXT, datos TEXT)")
    filas = con.execute("SELECT ticker, nombre, fecha, datos FROM analisis ORDER BY fecha DESC").fetchall()
    con.close()
    return filas


st.set_page_config(page_title="Analizador de empresas", layout="wide")
st.title("Analizador de empresas")
st.caption("Datos oficiales de la SEC (10-K presentados por cada empresa).")

email = st.sidebar.text_input("Tu email (la SEC lo exige para identificarte)")
ticker = st.text_input("Ticker de la empresa (por ejemplo, DPZ)").strip()

if st.button("Analizar") and ticker:
    if "@" not in email:
        st.error("Escribe tu email en la barra lateral.")
    else:
        try:
            cik, nombre = buscar_cik(ticker, email)
            if not cik:
                st.error("No encuentro ese ticker en la SEC.")
            else:
                facts = sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", email)
                tabla = construir_tabla(facts)
                guardar(ticker.upper(), nombre, tabla)
                st.subheader(f"{nombre} ({ticker.upper()})")
                st.caption("Millones de USD. Fuente: SEC EDGAR, formularios 10-K.")
                st.dataframe(tabla.T, use_container_width=True)
                st.line_chart(tabla[["Ventas", "Caja libre"]])
        except Exception as e:
            st.error(f"Ha habido un error: {e}")

st.divider()
st.subheader("Historial")
for t, n, f, datos in historial():
    with st.expander(f"{t} · {n} · {f}"):
        st.dataframe(pd.read_json(io.StringIO(datos)).T, use_container_width=True)
