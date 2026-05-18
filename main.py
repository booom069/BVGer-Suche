import json
import re
from dataclasses import dataclass
from html import unescape
from io import BytesIO
from time import sleep
from typing import Any

import fitz
import pandas as pd
import requests
import streamlit as st


# ============================================================
# Streamlit App
# ============================================================

st.set_page_config(page_title="BVGer Recherche", layout="wide")

st.title("BVGer Recherche")
st.info("Live-Recherche beim Bundesverwaltungsgericht mit Volltext-Export als CSV.")


# ============================================================
# Konstanten
# ============================================================

API_URL = "https://bvger.weblaw.ch/api/.netlify/functions/searchQueryService"
BASE_URL = "https://bvger.weblaw.ch"

HEADERS = {
    "accept": "*/*",
    "content-type": "text/plain;charset=UTF-8",
    "origin": "https://bvger.weblaw.ch",
    "referer": "https://bvger.weblaw.ch/dashboard?guiLanguage=de",
    "user-agent": "Mozilla/5.0",
}

REQUEST_TIMEOUT = 120
PAGE_SIZE = 50
SLEEP_BETWEEN_REQUESTS = 0.25

AGG_FIELDS = [
    "panel",
    "language",
    "rulingType",
    "subject",
    "bvgeKeywords",
    "bvgeStandards",
    "jud-ch-bund-bvgeList",
    "jud-ch-bund-bvgerList",
    "ch-jurivocList",
    "year",
    "lex-ch-bund-srList",
    "srCategoryList",
    "jud-ch-bund-bgeList",
    "jud-ch-bund-bguList",
    "jud-ch-bund-tpfList",
    "jud-ch-bund-bstgerList",
    "lex-ch-bund-asList",
    "lex-ch-bund-bblList",
    "lex-ch-bund-abList",
]


# ============================================================
# Dataclasses
# ============================================================

@dataclass
class PdfResult:
    text: str
    status: str
    error: str


@dataclass
class SearchResult:
    docs: list
    error: str
    status: str


# ============================================================
# Text- und Metadaten-Helfer
# ============================================================

def clean_text(text: Any) -> str:
    text = unescape(str(text or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_title(title: str) -> str:
    title = clean_text(title)

    parts = []
    seen = set()

    for part in re.split(r";+", title):
        part = part.strip()
        if part and part not in seen:
            seen.add(part)
            parts.append(part)

    return " | ".join(parts)


def first(meta: dict, key: str) -> str:
    value = meta.get(key, [""])

    if isinstance(value, list) and value:
        return str(value[0] or "")

    if isinstance(value, str):
        return value

    return ""


def all_values(meta: dict, key: str) -> str:
    value = meta.get(key, [])

    if isinstance(value, list):
        cleaned = []
        seen = set()

        for v in value:
            v = clean_text(v)
            if v and v not in seen:
                seen.add(v)
                cleaned.append(v)

        return "; ".join(cleaned)

    if isinstance(value, str):
        return clean_text(value)

    return ""


def safe_date(dates: dict, key: str) -> str:
    value = dates.get(key, "")

    if isinstance(value, list) and value:
        value = value[0]

    value = str(value or "")

    # Format aus 2025-09-10T02:00:00.000Z wird 2025-09-10
    if "T" in value:
        value = value.split("T")[0]

    return value


def pdf_url_from_original(original_url: str) -> str:
    if not original_url:
        return ""

    if original_url.startswith("http"):
        return original_url

    if original_url.startswith("/"):
        return BASE_URL + original_url

    return BASE_URL + "/" + original_url


def extract_business_number(title: str) -> str:
    match = re.search(r"\b[A-Z]-?\d{1,5}/\d{4}\b", title or "")
    return match.group(0) if match else ""


def make_doc_id(doc: dict) -> str:
    meta = doc.get("metadataKeywordTextMap", {})
    dates = doc.get("metadataDateMap", {})

    original_url = first(meta, "originalUrl")
    title = first(meta, "title")
    date = safe_date(dates, "rulingDate")
    number = first(meta, "reference")

    return original_url or number or f"{title}_{date}"


# ============================================================
# PDF-Extraktion
# ============================================================

def extract_pdf_text(pdf_url: str) -> PdfResult:
    if not pdf_url:
        return PdfResult("", "Keine PDF-URL", "")

    try:
        r = requests.get(
            pdf_url,
            headers={"user-agent": "Mozilla/5.0"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()

        content_type = r.headers.get("content-type", "").lower()

        if "pdf" not in content_type and not r.content.startswith(b"%PDF"):
            return PdfResult(
                "",
                "Keine PDF-Datei",
                f"Content-Type: {content_type}",
            )

        pdf = fitz.open(stream=BytesIO(r.content), filetype="pdf")

        pages = []
        for page in pdf:
            pages.append(page.get_text())

        text = "\n".join(pages).strip()

        if not text:
            return PdfResult("", "PDF ohne extrahierbaren Text", "")

        return PdfResult(text, "OK", "")

    except Exception as e:
        return PdfResult("", "PDF-Fehler", str(e))


# ============================================================
# Relevanz-Scoring
# ============================================================

def relevance_score(doc: dict, query: str) -> float:
    meta = doc.get("metadataKeywordTextMap", {})

    title = clean_title(first(meta, "title")).lower()
    snippet = clean_text(doc.get("content", "")).lower()
    text = f"{title} {snippet}"

    query_lower = query.lower().strip()
    words = [w.strip() for w in re.split(r"\s+", query_lower) if len(w.strip()) > 2]

    score = 0.0

    # Exakte Phrase ist besonders stark
    if query_lower and query_lower in text:
        score += 100

    # Einzelwörter zählen
    for word in words:
        count = text.count(word)
        score += count * 10

        if word in title:
            score += 30

    # Bonus, wenn mehrere Suchwörter gemeinsam vorkommen
    matched_words = sum(1 for word in words if word in text)

    if words:
        coverage = matched_words / len(words)
        score += coverage * 80

    # Bonus für BVGE-Leitentscheide
    if "bvge" in title:
        score += 50

    # Bonus für längere Suchauszüge
    score += min(len(snippet) / 100, 40)

    return round(score, 2)


# ============================================================
# API-Suche
# ============================================================

def build_payload(query: str, offset: int, size: int) -> dict:
    return {
        "queryString": query,
        "guiLanguage": "de",
        "userID": "_research",
        "sessionDuration": 43,
        "offset": offset,
        "size": size,
        "aggs": {
            "fields": AGG_FIELDS,
            "size": "10",
        },
    }


def search_bvger_api(query: str, max_hits: int) -> SearchResult:
    docs = []
    seen = set()

    offset = 0
    repeated_pages = 0
    last_error = ""

    while len(docs) < max_hits:
        payload = build_payload(query, offset, PAGE_SIZE)

        try:
            r = requests.post(
                API_URL,
                headers=HEADERS,
                data=json.dumps(payload),
                timeout=REQUEST_TIMEOUT,
            )

            if r.status_code != 200:
                last_error = f"HTTP {r.status_code}: {r.text[:500]}"
                break

            data = r.json()
            batch = data.get("documents", [])

            if not batch:
                break

            new_count = 0

            for doc in batch:
                doc_id = make_doc_id(doc)

                if doc_id and doc_id not in seen:
                    seen.add(doc_id)
                    docs.append(doc)
                    new_count += 1

                if len(docs) >= max_hits:
                    break

            if new_count == 0:
                repeated_pages += 1
            else:
                repeated_pages = 0

            if repeated_pages >= 2:
                break

            offset += PAGE_SIZE
            sleep(SLEEP_BETWEEN_REQUESTS)

        except Exception as e:
            last_error = str(e)
            break

    if docs and last_error:
        return SearchResult(docs, last_error, "Teilweise erfolgreich")

    if docs:
        return SearchResult(docs, "", "OK")

    if last_error:
        return SearchResult([], last_error, "Fehler")

    return SearchResult([], "", "Keine Treffer")


def search_bvger(query: str, max_hits: int) -> SearchResult:
    """
    Zentrale Suchfunktion.

    Aktuell:
    - Weblaw/BVGer-API

    Später möglich:
    - HTML-/Jurispub-Fallback
    - direkte Geschäftsnummernsuche
    - lokaler Cache
    """
    return search_bvger_api(query, max_hits)


# ============================================================
# Dokument in CSV-Zeile umwandeln
# ============================================================

def doc_to_row(doc: dict, search_query: str, load_fulltext: bool) -> dict:
    meta = doc.get("metadataKeywordTextMap", {})
    dates = doc.get("metadataDateMap", {})

    raw_title = first(meta, "title")
    title = clean_title(raw_title)
    date = safe_date(dates, "rulingDate")

    original_url = first(meta, "originalUrl")
    pdf_url = pdf_url_from_original(original_url)

    snippet = clean_text(doc.get("content", ""))

    pdf_result = PdfResult("", "Nicht geladen", "")

    if load_fulltext and pdf_url:
        pdf_result = extract_pdf_text(pdf_url)

    business_number = (
        first(meta, "reference")
        or first(meta, "fileNumber")
        or extract_business_number(title)
    )

    score = relevance_score(doc, search_query)

    return {
        "Titel": title,
        "Relevanz_Score": score,
        "Geschäftsnummer": business_number,
        "Datum": date,
        "Jahr": all_values(meta, "year"),
        "Sprache": all_values(meta, "language"),
        "Abteilung": all_values(meta, "panel"),
        "Entscheidtyp": all_values(meta, "rulingType"),
        "Rechtsgebiet": all_values(meta, "subject"),
        "Schlagworte": all_values(meta, "bvgeKeywords"),
        "Normen": all_values(meta, "bvgeStandards"),
        "SR": all_values(meta, "lex-ch-bund-srList"),
        "PDF": pdf_url,
        "OriginalUrl": original_url,
        "Suchbegriff": search_query,
        "Suchauszug": snippet,
        "Volltext_Status": pdf_result.status,
        "Volltext_Fehler": pdf_result.error,
        "Volltext_Zeichen": len(pdf_result.text),
        "Volltext": pdf_result.text,
    }


# ============================================================
# Streamlit UI
# ============================================================

query_text = st.text_area(
    "Suchbegriff / Thema",
    value="Türkei Politmalus",
    help="Mehrere Suchbegriffe untereinander eingeben. Jede Zeile wird separat gesucht.",
    height=140,
)

col1, col2, col3 = st.columns(3)

with col1:
    max_hits_per_query = st.number_input(
        "Maximale Treffer pro Suchbegriff",
        min_value=10,
        max_value=500,
        value=100,
        step=10,
    )

with col2:
    max_fulltexts_total = st.number_input(
        "Maximale PDF-Volltexte insgesamt",
        min_value=0,
        max_value=500,
        value=100,
        step=10,
    )

with col3:
    preview_count = st.slider(
        "Vorschau-Treffer",
        min_value=1,
        max_value=50,
        value=10,
    )

with st.expander("Optionen"):
    show_only_loaded = st.checkbox(
        "In der Vorschau nur Urteile mit geladenem Volltext zeigen",
        value=False,
    )

    st.write(
        """
        Die App sammelt zuerst Treffer, berechnet dann einen Relevanz-Score
        und lädt anschließend die PDF-Volltexte der besten Treffer zuerst.
        """
    )


# ============================================================
# Hauptprozess
# ============================================================

if st.button("Recherche starten und CSV erstellen", type="primary"):

    queries = [q.strip() for q in query_text.splitlines() if q.strip()]

    if not queries:
        st.warning("Bitte mindestens einen Suchbegriff eingeben.")
        st.stop()

    all_docs = []
    global_seen = set()
    search_log = []

    progress_search = st.progress(0)
    status = st.empty()

    # --------------------------------------------------------
    # 1. Suche
    # --------------------------------------------------------

    for idx, q in enumerate(queries, start=1):
        status.write(f"Suche {idx}/{len(queries)}: {q}")

        result = search_bvger(q, int(max_hits_per_query))

        search_log.append(
            {
                "Suchbegriff": q,
                "Status": result.status,
                "Treffer": len(result.docs),
                "Fehler": result.error,
            }
        )

        for doc in result.docs:
            doc_id = make_doc_id(doc)

            if doc_id and doc_id not in global_seen:
                global_seen.add(doc_id)
                all_docs.append((doc, q))

        progress_search.progress(idx / len(queries))

    st.success(f"{len(all_docs)} eindeutige Urteile gefunden.")

    st.subheader("Suchprotokoll")
    log_df = pd.DataFrame(search_log)
    st.dataframe(log_df, use_container_width=True)

    if not all_docs:
        st.warning("Keine verwertbaren Treffer gefunden.")
        st.stop()

    # --------------------------------------------------------
    # 2. Relevanzranking vor PDF-Download
    # --------------------------------------------------------

    ranked_docs = []

    for doc, q in all_docs:
        score = relevance_score(doc, q)
        ranked_docs.append((score, doc, q))

    ranked_docs.sort(reverse=True, key=lambda x: x[0])

    all_docs = [(doc, q) for score, doc, q in ranked_docs]

    st.info(
        "Treffer wurden nach inhaltlicher Relevanz sortiert. "
        "Die besten Treffer werden zuerst als Volltext geladen."
    )

    ranking_preview = []

    for score, doc, q in ranked_docs[:20]:
        meta = doc.get("metadataKeywordTextMap", {})
        dates = doc.get("metadataDateMap", {})

        ranking_preview.append(
            {
                "Score": score,
                "Titel": clean_title(first(meta, "title")),
                "Datum": safe_date(dates, "rulingDate"),
                "Suchbegriff": q,
                "PDF": pdf_url_from_original(first(meta, "originalUrl")),
            }
        )

    st.subheader("Top-Ranking vor PDF-Download")
    st.dataframe(pd.DataFrame(ranking_preview), use_container_width=True)

    # --------------------------------------------------------
    # 3. PDF-Volltexte laden
    # --------------------------------------------------------

    rows = []
    fulltext_attempts = 0
    fulltext_ok = 0

    progress_pdf = st.progress(0)
    pdf_status = st.empty()

    for i, (doc, q) in enumerate(all_docs, start=1):
        load_fulltext = fulltext_attempts < int(max_fulltexts_total)

        if load_fulltext:
            meta = doc.get("metadataKeywordTextMap", {})
            title = clean_title(first(meta, "title"))

            pdf_status.write(
                f"Extrahiere PDF {fulltext_attempts + 1}/{max_fulltexts_total}: {title}"
            )

            fulltext_attempts += 1

        row = doc_to_row(doc, q, load_fulltext)

        if row["Volltext_Status"] == "OK":
            fulltext_ok += 1

        rows.append(row)

        progress_pdf.progress(i / len(all_docs))

    df = pd.DataFrame(rows)

    # Nach Score sortiert lassen
    df = df.sort_values(by="Relevanz_Score", ascending=False).reset_index(drop=True)

    # --------------------------------------------------------
    # 4. Resultate anzeigen und CSV exportieren
    # --------------------------------------------------------

    st.success(f"{len(df)} Urteile verarbeitet.")
    st.info(f"{fulltext_ok} PDF-Volltexte erfolgreich extrahiert.")

    csv = df.to_csv(index=False).encode("utf-8-sig")

    st.download_button(
        "CSV herunterladen",
        data=csv,
        file_name="bvger_recherche_export.csv",
        mime="text/csv",
    )

    st.subheader("Resultate")
    st.dataframe(df, use_container_width=True)

    st.subheader("Vorschau")

    preview_df = df.copy()

    if show_only_loaded:
        preview_df = preview_df[preview_df["Volltext_Status"] == "OK"]

    for idx, row in preview_df.head(preview_count).iterrows():
        st.markdown(f"### {row['Titel']}")
        st.write("Relevanz-Score:", row["Relevanz_Score"])
        st.write("Datum:", row["Datum"])
        st.write("Geschäftsnummer:", row["Geschäftsnummer"])
        st.write("Volltext-Status:", row["Volltext_Status"])

        if row["PDF"]:
            st.markdown(f"[PDF öffnen]({row['PDF']})")

        text = row["Volltext"] if row["Volltext"] else row["Suchauszug"]

        st.text_area(
            "Vorschau",
            str(text)[:4000],
            height=250,
            key=f"preview_{idx}",
        )

        st.divider()
