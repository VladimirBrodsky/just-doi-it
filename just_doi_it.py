import os
import streamlit as st
import requests
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from collections import defaultdict

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

def parse_seed_inputs(raw: str) -> list[str]:
    """Accept newline/comma/space-separated list; normalize & dedupe, keep order."""
    if not raw:
        return []
    # Split on newline, comma, semicolon, or whitespace runs
    import re
    parts = re.split(r"[,\n;\t ]+", raw.strip())
    seen = set()
    out = []
    for p in parts:
        d = extract_doi(p)
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out

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
st.caption("Paste one **or many** DOIs/URLs (one per line is fine), fetch their references, tick the ones you want, and download.")

# Inputs
c_top = st.columns([3, 1, 1])
with c_top[0]:
    doi_input_multi = st.text_area(
        "Enter one or multiple DOIs/URLs",
        value=DEFAULT_DOI_URL,
        height=96,
        placeholder="One per line, or separated by commas/spaces",
    )
with c_top[1]:
    citation_format = st.selectbox("Format", ["BibTeX", "RIS", "EndNote"])
with c_top[2]:
    max_refs = st.number_input("Max refs per seed", min_value=5, max_value=500, value=100, step=5)

# Parallelism slider (optional tuning)
max_workers = st.slider(
    "Parallel requests",
    min_value=4,
    max_value=32,
    value=MAX_WORKERS_DEFAULT,
    step=2,
    help="Higher is faster but may hit API rate limits."
)

fetch_clicked = st.button("Fetch References", type="primary")

# Session state
st.session_state.setdefault("items", [])  # [{id, doi, label, sources(set), citation}]
st.session_state.setdefault("selected_ids", set())
st.session_state.setdefault("active_fmt", citation_format)
st.session_state.setdefault("seed_order", [])  # keep order of seeds for filtering

if fetch_clicked:
    st.session_state["items"] = []
    st.session_state["selected_ids"] = set()
    st.session_state["active_fmt"] = citation_format
    seeds = parse_seed_inputs(doi_input_multi)
    st.session_state["seed_order"] = seeds

    if not seeds:
        st.warning("Please enter at least one valid DOI or DOI URL.")
    else:
        # 1) Fetch refs for each seed (parallel)
        with st.spinner("Contacting Crossref…"):
            refs_by_seed = {}
            errors = []
            progress = st.progress(0.0, text="Fetching reference lists…")

            def task(seed):
                try:
                    return seed, crossref_refs_for_work(seed)
                except Exception as e:
                    return seed, e

            results = []
            with ThreadPoolExecutor(max_workers=min(max_workers, max(4, len(seeds)))) as ex:
                futures = [ex.submit(task, s) for s in seeds]
                total = len(futures)
                done = 0
                for fut in as_completed(futures):
                    results.append(fut.result())
                    done += 1
                    progress.progress(done / total, text=f"Fetched {done}/{total} reference lists")

            progress.empty()

            for seed, r in results:
                if isinstance(r, Exception):
                    errors.append((seed, r))
                else:
                    refs_by_seed[seed] = r

            if errors:
                for seed, e in errors:
                    st.error(f"Failed to fetch references for {seed}: {e}")

        # 2) Build unique set of referenced DOIs, while tracking which seeds cited each
        if refs_by_seed:
            unique_raw_by_doi = {}
            seeds_for_doi = defaultdict(set)
            total_refs_before_dedupe = 0

            for seed, ref_list in refs_by_seed.items():
                # Limit per seed
                ref_list = (ref_list or [])[: int(max_refs)]
                total_refs_before_dedupe += len(ref_list)
                for ref in ref_list:
                    d = ref.get("doi")
                    if not d:
                        continue
                    if d not in unique_raw_by_doi:
                        unique_raw_by_doi[d] = ref.get("raw") or {}
                    seeds_for_doi[d].add(seed)

            if not unique_raw_by_doi:
                st.info("No references with DOIs were found among the provided seeds.")
            else:
                # 3) Fetch metadata + formatted citations for unique referenced DOIs (parallel)
                items = []
                with st.spinner("Fetching metadata & citations…"):
                    progress = st.progress(0.0, text="Starting…")
                    futures = {}
                    dois = list(unique_raw_by_doi.keys())
                    with ThreadPoolExecutor(max_workers=max_workers) as ex:
                        for d in dois:
                            futures[ex.submit(
                                fetch_item_for_doi, d, unique_raw_by_doi[d], citation_format
                            )] = d
                        total = len(futures)
                        done = 0
                        for fut in as_completed(futures):
                            res = fut.result()
                            did = futures[fut]
                            done += 1
                            progress.progress(done / total, text=f"Fetched {done}/{total}")
                            if res:
                                res["sources"] = seeds_for_doi[did]
                                items.append(res)
                    progress.empty()

                # Assign ids after parallel stage
                for i, it in enumerate(items):
                    it["id"] = i

                st.session_state["items"] = items

        else:
            st.info("No references found.")

# --------------- Selection + Download ---------------
items = st.session_state.get("items", [])
seed_order = st.session_state.get("seed_order", [])

if items:
    st.subheader("Select references to export")

    # Optional: filter by which seed(s) cited the reference
    if seed_order:
        with st.expander("Filter by source paper(s)", expanded=False):
            selected_seeds = st.multiselect(
                "Show references cited by these seed(s):",
                options=seed_order,
                default=seed_order,
                help="Use this to narrow the list to refs that came from specific seed papers."
            )
    else:
        selected_seeds = []

    # Bulk actions
    c_btn = st.columns([1, 1, 2])
    with c_btn[0]:
        if st.button("Select all"):
            st.session_state["selected_ids"] = {it["id"] for it in items}
    with c_btn[1]:
        if st.button("Clear all"):
            st.session_state["selected_ids"] = set()
    with c_btn[2]:
        st.caption(f"Format locked to **{st.session_state['active_fmt']}** for this fetch.")

    # Apply source filter (if any)
    def passes_seed_filter(it) -> bool:
        if not seed_order or not selected_seeds:
            return True
        return bool(set(selected_seeds) & set(it.get("sources", set())))

    filtered_items = [it for it in items if passes_seed_filter(it)]

    if not filtered_items:
        st.info("No references match the current source filter.")
    else:
        # Checkboxes
        for it in filtered_items:
            sources_note = ""
            if it.get("sources"):
                # Show a compact list of sources that cited this ref
                srcs = ", ".join(it["sources"])
                sources_note = f"\n\n*From:* {srcs}"
            checked = st.checkbox(
                it["label"] + sources_note,
                key=f"chk_{it['id']}",
                value=it["id"] in st.session_state["selected_ids"],
            )
            if checked:
                st.session_state["selected_ids"].add(it["id"])
            else:
                st.session_state["selected_ids"].discard(it["id"])

        selected = [it for it in items if it["id"] in st.session_state["selected_ids"] and it in filtered_items]

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
            st.info("Tick some references above, or adjust the source filter.")
else:
    st.info("Enter one or many DOIs above and click **Fetch References** to begin.")
