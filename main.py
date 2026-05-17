import sqlite3
import json
import re
from html import unescape
from io import BytesIO
from time import sleep

import fitz
import pandas as pd
import requests
import streamlit as st


URL = "https://bvger.weblaw.ch/api/.netlify/functions/searchQueryService"

HEADERS = {
    "accept": "*/*",
    "content-type": "text/plain;charset=UTF-8",
    "origin": "https://bvger.weblaw.ch",
    "referer": "https://bvger.weblaw.ch/dashboard",
    "user-agent": "Mozilla/5.0",
}

DB_PATH = "bvger_recherche.db"

AGG_FIELDS = [
    "panel",
    "language",
    "rulingType",
    "subject",
    "year",
]


conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS urteile (
    doc_id TEXT PRIMARY KEY,
    titel TEXT,
    datum TEXT,
    pdf TEXT,
    suchbegriff TEXT,
    suchauszug TEXT,
    volltext TEXT
)
""")
conn.commit()


def clean_text(text):
    text = unescape(str(text or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def first(meta, key):
    value = meta.get(key, [""])
    if isinstance(value, list) and value:
        return value[0]
    return ""


def bvger_request(payload):
    r = requests.post(
        URL,
        headers=HEADERS,
        data=json.dumps(payload),
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def make_doc_id(doc):
    meta = doc.get("metadataKeywordTextMap", {})
    original_url = first(meta, "originalUrl")

    if original_url:
        return original_url

    leid = doc.get("leid", "")
    if leid:
        return leid

    titel = first(meta, "title")
    datum = doc.get("metadataDateMap", {}).get("rulingDate", "")
    return titel + "_" + str(datum)


def search_bvger(query, max_hits):
    all_docs = []
    seen = set()

    offset = 0
    size = 50

    progress = st.progress(0)
    status = st.empty()

    while len(all_docs) < max_hits:
        payload = {
            "queryString": query,
            "guiLanguage": "de",
            "userID": "_research",
            "sessionDuration": 43,
            "offset": offset,
            "from": offset,
            "size": size,
            "aggs": {
                "fields": AGG_FIELDS,
                "size": "10",
            },
        }

        data = bvger_request(payload)
        batch = data.get("documents", [])

        if not batch:
            break

        new_in_batch = 0

        for doc in batch:
            doc_id = make_doc_id(doc)

            if doc_id and doc_id not in seen:
                seen.add(doc_id)
                all_docs.append(doc)
                new_in_batch += 1

            if len(all_docs) >= max_hits:
                break

        status.write(f"Geladen: {len(all_docs)} Treffer")
        progress.progress(min(len(all_docs) / max_hits, 1.0))

        if new_in_batch == 0:
            break

        offset += size
        sleep(0.2)

    progress.progress(1.0)
    return all_docs


def pdf_to_text(pdf_url):
    try:
        r = requests.get(
            pdf_url,
            headers={"user-agent": "Mozilla/5.0"},
            timeout=120,
        )
        r.raise_for_status()

        pdf = fitz.open(stream=BytesIO(r.content), filetype="pdf")
        pages = []

        for page in pdf:
            pages.append(page.get_text())

        return "\n".join(pages).strip()

    except Exception as e:
        return f"PDF_FEHLER: {e}"


def save_docs(docs, query, load_fulltexts, max_fulltexts):
    inserted = 0
    updated = 0
    fulltext_counter = 0

    progress = st.progress(0)
    status = st.empty()

    for i, doc in enumerate(docs, start=1):
        meta = doc.get("metadataKeywordTextMap", {})
        dates = doc.get("metadataDateMap", {})

        doc_id = make_doc_id(doc)
        titel = clean_text(first(meta, "title"))
        datum = str(dates.get("rulingDate", ""))
        original_url = first(meta, "originalUrl")

        pdf = ""
        if original_url:
            if original_url.startswith("http"):
                pdf = original_url
            else:
                pdf = "https://bvger.weblaw.ch" + original_url

        suchauszug = clean_text(doc.get("content", ""))
        volltext = ""

        cur.execute(
            "SELECT doc_id, volltext FROM urteile WHERE doc_id=?",
            (doc_id,)
        )
        existing = cur.fetchone()

        if existing:
            existing_fulltext = existing[1] or ""

            if load_fulltexts and pdf and not existing_fulltext and fulltext_counter < max_fulltexts:
                status.write(f"PDF-Volltext ergänzen {fulltext_counter + 1}/{max_fulltexts}: {titel}")
                volltext = pdf_to_text(pdf)
                fulltext_counter += 1

                cur.execute("""
                UPDATE urteile
                SET volltext=?, suchbegriff=?
                WHERE doc_id=?
                """, (
                    volltext,
                    query,
                    doc_id,
                ))

                updated += 1
                conn.commit()

            progress.progress(i / len(docs))
            continue

        if load_fulltexts and pdf and fulltext_counter < max_fulltexts:
            status.write(f"PDF-Volltext {fulltext_counter + 1}/{max_fulltexts}: {titel}")
            volltext = pdf_to_text(pdf)
            fulltext_counter += 1

        cur.execute("""
        INSERT OR IGNORE INTO urteile (
            doc_id,
            titel,
            datum,
            pdf,
            suchbegriff,
            suchauszug,
            volltext
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            doc_id,
            titel,
            datum,
            pdf,
            query,
            suchauszug,
            volltext,
        ))

        if cur.rowcount > 0:
            inserted += 1

        conn.commit()
        progress.progress(i / len(docs))

    progress.progress(1.0)
    return inserted, updated


def local_search(query):
    terms = [t.strip() for t in query.split(" OR ") if t.strip()]

    if not terms:
        return pd.DataFrame()

    conditions = []
    params = []

    for term in terms:
        like = f"%{term}%"
        conditions.append(
            "(titel LIKE ? OR suchauszug LIKE ? OR volltext LIKE ? OR suchbegriff LIKE ?)"
        )
        params.extend([like, like, like, like])

    sql = f"""
    SELECT *
    FROM urteile
    WHERE {" OR ".join(conditions)}
    ORDER BY datum DESC
    """

    return pd.read_sql_query(sql, conn, params=params)


st.set_page_config(page_title="BVGer Recherche", layout="wide")

st.title("BVGer Recherche")

tab1, tab2 = st.tabs([
    "1. Urteile laden",
    "2. Volltextsuche & CSV Export",
])


with tab1:
    st.subheader("BVGer live durchsuchen und Volltexte speichern")

    query = st.text_input("Suchbegriff / Thema", "")

    max_hits = st.number_input(
        "Maximale Treffer von BVGer holen",
        min_value=10,
        max_value=500,
        value=50,
        step=10,
    )

    load_fulltexts = st.checkbox(
        "PDF-Volltexte extrahieren",
        value=True,
    )

    max_fulltexts = st.number_input(
        "Maximale PDF-Volltexte speichern",
        min_value=1,
        max_value=200,
        value=20,
        step=5,
    )

    if st.button("Urteile laden und speichern"):
        if not query.strip():
            st.warning("Bitte Suchbegriff eingeben.")
            st.stop()

        with st.spinner("BVGer wird durchsucht..."):
            docs = search_bvger(query.strip(), int(max_hits))

        st.success(f"{len(docs)} Treffer gefunden.")

        with st.spinner("Urteile werden gespeichert / PDFs werden extrahiert..."):
            inserted, updated = save_docs(
                docs,
                query.strip(),
                load_fulltexts,
                int(max_fulltexts),
            )

        st.success(
            f"{inserted} neue Urteile gespeichert. "
            f"{updated} bestehende Urteile mit Volltext ergänzt."
        )


with tab2:
    st.subheader("Lokale Volltextsuche und Export")

    local_query = st.text_input(
        "Suche in gespeicherten Urteilen",
        "",
        help="Mehrere Begriffe mit OR trennen, z.B. Asyl OR asile OR asilo",
    )

    limit_preview = st.slider(
        "Anzahl Vorschau-Treffer anzeigen",
        1,
        50,
        10,
    )

    if local_query.strip():
        df = local_search(local_query.strip())

        st.success(f"{len(df)} gespeicherte Treffer gefunden.")

        csv = df.to_csv(index=False).encode("utf-8-sig")

        st.download_button(
            "CSV herunterladen",
            data=csv,
            file_name="bvger_recherche_export.csv",
            mime="text/csv",
            key="recherche_export_csv",
        )

        for _, row in df.head(limit_preview).iterrows():
            st.markdown(f"### {row['titel']}")
            st.write("Datum:", row["datum"])

            if row["pdf"]:
                st.markdown(f"[PDF öffnen]({row['pdf']})")

            text = row["volltext"] if row["volltext"] else row["suchauszug"]

            st.text_area(
                "Vorschau",
                str(text)[:4000],
                height=300,
            )

            st.divider()


count_df = pd.read_sql_query("SELECT COUNT(*) AS total FROM urteile", conn)
total_count = int(count_df.iloc[0]["total"])

st.sidebar.success(f"Datenbank: {total_count} Urteile")
