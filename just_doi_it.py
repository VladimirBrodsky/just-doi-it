import os
import streamlit as st
import requests
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ---------------- Config ----------------
APP_TITLE = "Just DOI it!"
DEFAULT_DOI_URL = "https://doi.org/10.1016/j.stress.2025.100958"
MAX_TITLE_LENGTH = 90
REQUEST_TIMEOUT = 10  # seconds
MAX_WORKERS_DEFAULT = min(16, (os.cpu_count() or 4) * 2)  # parallelism

st.set_page_config(page_title=APP_TITLE, layout="centered")

# ---------------- HTTP Session w/ retries ----------------
def make_session():
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    # Be polite with Crossref (add your email if you like)
    s.headers.update({"User-Agent": "JustDOIit/1.0 (mailto:you@example.com)"})
    return s

SESSION = make_session()

# ---------------- Helpers ----------------
def extract_doi(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return s
    if "doi.org" in s:
        return urlparse(s).path.lstrip("/")
    return s

def short(s: str, n: int = MAX_TITLE_LENGTH) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"

def file_ext(fmt: str) -> str:
    return {"BibTeX": "bib", "RIS": "ris", "EndNote": "enw"}.get(fmt, "txt")

def authors_label(authors):
    fams = [a.get("family") for a in (authors or []) if a.get("family")]
    if not fams:
        return "Unknown"
    if len(fams) == 1:
        return fams[0]
    if len(fams) == 2:
        return f"{fams[0]} & {fams[1]}"
    return f"{fams[0]} et al."

# ---------------- Cached fetchers ----------------
@st.cache_data(show_spinner=False)
def crossref_refs_for_work(doi: str):
    r = SESSION.get(f"https://api.crossref.org/works/{doi}", timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    refs = (r.json().get("message", {}) or {}).get("reference", []) or []
    seen, out = set(), []
    for ref in refs:
        d = (ref or {}).get("DOI")
        if d:
            d = d.strip()
            if d and d not in seen:
                seen.add(d)
                out.append({"raw": ref, "doi": d})
    return out

@st.cache_data(show_spinner=False)
def crossref_meta_for_doi(doi: str):
    r = SESSION.get(f"https://api.crossref.org/works/{doi}", timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    msg = r.json().get("message", {}) or {}

    # title
    title = None
    if isinstance(msg.get("title"), list) and msg["title"]:
        title = msg["title"][0]

    # year (try several fields)
    year = None
    for key in ("issued", "published-print", "published-online", "created"):
        part = msg.get(key) or {}
        dp = part.get("date-parts")
        if isinstance(dp, list) and dp and isinstance(dp[0], list) and dp[0]:
            year = dp[0][0]
            break

    # authors
    authors = []
    for a in msg.get("author", []) or []:
        fam = a.get("family") or a.get("familyName") or ""
        given = a.get("given") or ""
        if fam or given:
            authors.append({"given": given, "family": fam})
    return {"authors": authors, "year": year, "title": title}

@st.cache_data(show_spinner=False)
def formatted_citation(doi: str, fmt: str):
    mime = {
        "BibTeX": "application/x-bibtex",
        "RIS": "application/x-research-info-systems",
        "EndNote": "application/x-endnote-refer",
    }[fmt]
    r = SESSION.get(f"https://doi.org/{doi}", headers={"Accept": mime}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text

# Combined fetch for a single DOI (metadata + citation) so we can run it in parallel
def fetch_item_for_doi(rdoi: str, raw_ref, fmt: str):
    try:
        meta = crossref_meta_for_doi(rdoi)
        a_lbl = authors_label(meta["authors"])
        y_lbl = str(meta["year"]) if meta["year"] else "n.d."
        t_lbl = meta["title"] or (raw_ref.get("unstructured") if raw_ref else "") or rdoi
        label = f"{a_lbl}, {y_lbl} — {short(t_lbl)}"
        cit = formatted_citation(rdoi, fmt).strip()
        return {"doi": rdoi, "label": label, "citation": cit}
    except Exception:
        return None

# ---------------- UI ----------------
st.title(APP_TITLE)
st.caption("Paste a DOI/URL, fetch its references, tick the ones you want, and download.")

# Inputs
c_top = st.columns([3, 1, 1])
with c_top[0]:
    doi_input = st.text_input("Enter DOI or DOI URL", value=DEFAULT_DOI_URL, placeholder=DEFAULT_DOI_URL)
with c_top[1]:
    citation_format = st.selectbox("Format", ["BibTeX", "RIS", "EndNote"])
with c_top[2]:
    max_refs = st.number_input("Max refs", min_value=5, max_value=500, value=100, step=5)

# Parallelism slider (optional tuning)
max_workers = st.slider("Parallel requests", min_value=4, max_value=32, value=MAX_WORKERS_DEFAULT, step=2,
                        help="Higher is faster but may hit API rate limits.")

fetch_clicked = st.button("Fetch References", type="primary")

# Session state
st.session_state.setdefault("items", [])            # [{id, doi, label, citation}]
st.session_state.setdefault("selected_ids", set())  # set of ids
st.session_state.setdefault("active_fmt", citation_format)

if fetch_clicked:
    st.session_state["items"] = []
    st.session_state["selected_ids"] = set()
    st.session_state["active_fmt"] = citation_format

    doi = extract_doi(doi_input)
    if not doi:
        st.warning("Please enter a valid DOI or DOI URL.")
    else:
        with st.spinner("Contacting Crossref…"):
            try:
                refs = crossref_refs_for_work(doi)
            except Exception as e:
                st.error(f"Failed to fetch references for `{doi}`. {e}")
                refs = []

        if refs:
            # Limit count for speed if user requested
            refs = refs[: int(max_refs)]

            progress = st.progress(0.0, text="Fetching metadata & citations…")
            items = []
            futures = {}
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                for ref in refs:
                    futures[ex.submit(fetch_item_for_doi, ref["doi"], ref["raw"], citation_format)] = ref["doi"]

                total = len(futures)
                done_count = 0
                for fut in as_completed(futures):
                    res = fut.result()
                    done_count += 1
                    progress.progress(done_count / total, text=f"Fetched {done_count}/{total}")
                    if res:
                        items.append(res)
            progress.empty()

            # Assign ids after parallel stage
            for i, it in enumerate(items):
                it["id"] = i

            st.session_state["items"] = items
            if not items:
                st.info("No downloadable citations were found among the references.")
        else:
            st.info("No references with DOIs were found for that work.")

# --------------- Selection + Download ---------------
items = st.session_state["items"]
if items:
    st.subheader("Select references to export")

    c_btn = st.columns([1, 1])
    with c_btn[0]:
        if st.button("Select all"):
            st.session_state["selected_ids"] = {it["id"] for it in items}
    with c_btn[1]:
        if st.button("Clear all"):
            st.session_state["selected_ids"] = set()

    # Checkboxes
    for it in items:
        checked = st.checkbox(it["label"], key=f"chk_{it['id']}", value=it["id"] in st.session_state["selected_ids"])
        if checked:
            st.session_state["selected_ids"].add(it["id"])
        else:
            st.session_state["selected_ids"].discard(it["id"])

    selected = [it for it in items if it["id"] in st.session_state["selected_ids"]]
    if selected:
        preview = "\n\n".join(it["citation"] for it in selected)
        st.text_area("Preview selected", preview, height=260)
        st.download_button(
            label=f"Download selected ({st.session_state['active_fmt']})",
            data=preview,
            file_name=f"selected_references.{file_ext(st.session_state['active_fmt'])}",
            mime="text/plain",
        )
else:
    st.info("Enter a DOI above and click **Fetch References** to begin.")
