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

AGG_FIELDS = [
    "panel",
    "language",
    "rulingType",
    "subject",
    "bvgeKeywords",
    "bvgeStandards",
    "year",
]


def clean_text(text):
    text = unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def first(meta, key):
    value = meta.get(key, [""])
    if isinstance(value, list) and value:
        return value[0]
    return ""


def search_page(query, offset, size):
    payload = {
        "queryString": query,
        "guiLanguage": "de",
        "userID": "_2eiu90t",
        "sessionDuration": 43,
        "offset": offset,
        "size": size,
        "aggs": {
            "fields": AGG_FIELDS,
            "size": "10",
        },
    }

    r = requests.post(
        URL,
        headers=HEADERS,
        data=json.dumps(payload),
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def load_all_hits(query):
    all_docs = []
    seen = set()
    offset = 0
    size = 50
    total = None

    progress = st.progress(0)
    status = st.empty()

    while True:
        data = search_page(query, offset, size)

        if total is None:
            total = data.get("totalNumberOfDocuments", 0)

        docs = data.get("documents", [])
        if not docs:
            break

        for doc in docs:
            meta = doc.get("metadataKeywordTextMap", {})
            original_url = first(meta, "originalUrl")
            doc_id = original_url or str(doc.get("leid", "")) or str(doc)[:200]

            if doc_id not in seen:
                seen.add(doc_id)
                all_docs.append(doc)

        loaded = len(all_docs)
        status.write(f"Geladen: {loaded} von ca. {total}")

        if total:
            progress.progress(min(loaded / total, 1.0))

        if total and loaded >= total:
            break

        offset += size
        sleep(0.2)

    progress.progress(1.0)
    return all_docs, total


def pdf_to_text(pdf_url):
    try:
        r = requests.get(pdf_url, headers={"user-agent": "Mozilla/5.0"}, timeout=120)
        r.raise_for_status()

        pdf = fitz.open(stream=BytesIO(r.content), filetype="pdf")
        pages = []

        for page in pdf:
            pages.append(page.get_text())

        return "\n".join(pages).strip()

    except Exception as e:
        return f"PDF_FEHLER: {e}"


def docs_to_dataframe(docs, load_fulltexts, max_fulltexts):
    rows = []

    progress = st.progress(0)
    status = st.empty()

    for i, doc in enumerate(docs, start=1):
        meta = doc.get("metadataKeywordTextMap", {})
        dates = doc.get("metadataDateMap", {})

        title = first(meta, "title")
        original_url = first(meta, "originalUrl")
        ruling_date = dates.get("rulingDate", "")
        snippet = clean_text(doc.get("content", ""))

        pdf_url = ""
        if original_url:
            pdf_url = "https://bvger.weblaw.ch" + original_url

        fulltext = ""

        if load_fulltexts and pdf_url and i <= max_fulltexts:
            status.write(f"Extrahiere PDF-Volltext {i} von {min(len(docs), max_fulltexts)}")
            fulltext = pdf_to_text(pdf_url)

        rows.append(
            {
                "Titel": title,
                "Datum": ruling_date,
                "PDF": pdf_url,
                "Suchauszug": snippet,
                "Volltext": fulltext,
            }
        )

        progress.progress(i / len(docs))

    progress.progress(1.0)
    return pd.DataFrame(rows)


st.set_page_config(page_title="BVGer-Volltextsuche", layout="wide")
st.title("BVGer-Volltextsuche")

query = st.text_input("Suchbegriff", "")

preview_count = st.slider("Anzahl Vorschau-Treffer", 1, 20, 5)

load_fulltexts = st.checkbox("PDF-Volltexte extrahieren", value=False)

max_fulltexts = st.number_input(
    "Maximale Anzahl PDF-Volltexte",
    min_value=1,
    max_value=1000,
    value=20,
    step=10,
)

if st.button("Suche starten"):
    if not query.strip():
        st.warning("Bitte Suchbegriff eingeben.")
        st.stop()

    with st.spinner("BVGer-Treffer werden geladen..."):
        docs, total = load_all_hits(query.strip())

    st.success(f"{len(docs)} Treffer geladen. Gesamt laut BVGer: {total}")

    with st.spinner("Daten werden vorbereitet..."):
        df = docs_to_dataframe(docs, load_fulltexts, max_fulltexts)

    st.success("Export bereit.")

    for _, row in df.head(preview_count).iterrows():
        st.subheader(row["Titel"])
        st.write("Datum:", row["Datum"])

        if row["PDF"]:
            st.markdown(f"[PDF öffnen]({row['PDF']})")

        text = row["Volltext"] if row["Volltext"] else row["Suchauszug"]
        st.text_area("Vorschau", text[:4000], height=300)
        st.divider()

    st.download_button(
        "CSV herunterladen",
        df.to_csv(index=False).encode("utf-8-sig"),
        file_name="bvger_volltexte.csv",
        mime="text/csv",
    )
