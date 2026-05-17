import requests
import json
import pandas as pd
import streamlit as st
from html import unescape
from time import sleep

st.set_page_config(layout="wide")

URL = "https://bvger.weblaw.ch/api/.netlify/functions/searchQueryService"

HEADERS = {
    "accept": "*/*",
    "content-type": "text/plain;charset=UTF-8",
    "origin": "https://bvger.weblaw.ch",
    "referer": "https://bvger.weblaw.ch/dashboard",
    "user-agent": "Mozilla/5.0"
}

AGG_FIELDS = [
    "panel",
    "language",
    "rulingType",
    "subject",
    "bvgeKeywords",
    "bvgeStandards",
    "year"
]

st.title("BVGer-Urteilssuche")

query = st.text_input(
    "Suchbegriff",
    value="Türkei Strafverfolgung"
)

preview_count = st.slider(
    "Anzahl angezeigte Vorschau-Treffer",
    1,
    20,
    5
)

if st.button("Suche / alle laden"):

    all_documents = []

    offset = 0
    batch_size = 50
    total_hits = None

    progress = st.progress(0)

    while True:

        payload = {
            "queryString": query,
            "guiLanguage": "de",
            "userID": "_2eiu90t",
            "sessionDuration": 43,

            "offset": offset,
            "size": batch_size,

            "aggs": {
                "fields": AGG_FIELDS,
                "size": "10"
            }
        }

        response = requests.post(
            URL,
            headers=HEADERS,
            data=json.dumps(payload),
            timeout=120
        )

        if response.status_code != 200:
            st.error(f"Fehler {response.status_code}")
            st.text(response.text)
            st.stop()

        data = response.json()

        documents = data.get("documents", [])

        if total_hits is None:
            total_hits = data.get("totalNumberOfDocuments", 0)

        if not documents:
            break

        all_documents.extend(documents)

        loaded = len(all_documents)

        progress.progress(min(loaded / total_hits, 1.0))

        st.write(f"Geladen: {loaded} von ca. {total_hits}")

        if loaded >= total_hits:
            break

        offset += batch_size

        sleep(0.2)

    st.success(
        f"{len(all_documents)} Urteile vollständig geladen"
    )

    export_rows = []

    for i, doc in enumerate(all_documents[:preview_count], start=1):

        metadata = doc.get("metadataKeywordTextMap", {})
        dates = doc.get("metadataDateMap", {})

        title = metadata.get("title", ["Ohne Titel"])[0]

        original_url = metadata.get("originalUrl", [""])[0]

        ruling_date = dates.get("rulingDate", "")

        content = doc.get("content", "")

        content = unescape(content)

        content = (
            content
            .replace("<p>", "\n")
            .replace("</p>", "")
            .replace("<b>", "")
            .replace("</b>", "")
            .replace("<em>", "")
            .replace("</em>", "")
            .replace("<hr>", "\n---\n")
        )

        st.markdown("---")

        st.subheader(title)

        st.write(f"Datum: {ruling_date}")

        if original_url:
            st.markdown(
                f"[PDF öffnen](https://bvger.weblaw.ch{original_url})"
            )

        st.text(content[:1500])

    for doc in all_documents:

        metadata = doc.get("metadataKeywordTextMap", {})
        dates = doc.get("metadataDateMap", {})

        title = metadata.get("title", [""])[0]

        original_url = metadata.get("originalUrl", [""])[0]

        ruling_date = dates.get("rulingDate", "")

        content = doc.get("content", "")

        content = unescape(content)

        content = (
            content
            .replace("<p>", " ")
            .replace("</p>", " ")
            .replace("<b>", "")
            .replace("</b>", "")
            .replace("<em>", "")
            .replace("</em>", "")
        )

        export_rows.append({
            "Titel": title,
            "Datum": ruling_date,
            "URL": f"https://bvger.weblaw.ch{original_url}",
            "Text": content
        })

    df = pd.DataFrame(export_rows)

    csv = df.to_csv(
        index=False
    ).encode("utf-8-sig")

    st.download_button(
        "ALLE Treffer als CSV herunterladen",
        csv,
        file_name="bvger_export.csv",
        mime="text/csv"
    )