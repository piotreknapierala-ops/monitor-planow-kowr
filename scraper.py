from __future__ import annotations

import io
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pdfplumber
import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz
from unidecode import unidecode

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover
    fitz = None

PLAN_PAGE = "https://www.gov.pl/web/kowr/plan-postepowan"
OPEN_AUCTIONS = "https://kowr.eb2b.com.pl/open-auctions.html"
RESULTS_AUCTIONS = "https://kowr.eb2b.com.pl/auction-result-publication.html"
CURRENT_YEAR = int(os.getenv("MONITOR_YEAR", str(date.today().year)))
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
STATE_FILE = DATA_DIR / "state.json"
OUTPUT_FILE = DATA_DIR / "plans.json"
WEB_OUTPUT_FILE = DOCS_DIR / "data.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.6"}

ROMAN_QUARTERS = {"I": 1, "II": 2, "III": 3, "IV": 4}
MONTHS = {
    "styczen": 1, "stycznia": 1, "luty": 2, "lutego": 2, "marzec": 3, "marca": 3,
    "kwiecien": 4, "kwietnia": 4, "maj": 5, "maja": 5, "czerwiec": 6, "czerwca": 6,
    "lipiec": 7, "lipca": 7, "sierpien": 8, "sierpnia": 8, "wrzesien": 9, "wrzesnia": 9,
    "pazdziernik": 10, "pazdziernika": 10, "listopad": 11, "listopada": 11,
    "grudzien": 12, "grudnia": 12,
}
EXPECTED_UNITS = [
    "Centrala", "OT Białystok", "OT Bydgoszcz", "OT Częstochowa", "OT Gorzów Wielkopolski",
    "OT Kielce", "OT Koszalin", "OT Kraków", "OT Lublin", "OT Łódź", "OT Olsztyn", "OT Opole",
    "OT Poznań", "OT Pruszcz Gdański", "OT Rzeszów", "OT Szczecin", "OT Warszawa", "OT Wrocław",
]


VOIVODESHIP_BY_UNIT = {
    "Centrala": "Nieustalone",
    "OT Białystok": "podlaskie",
    "OT Bydgoszcz": "kujawsko-pomorskie",
    "OT Częstochowa": "śląskie",
    "OT Gorzów Wielkopolski": "lubuskie",
    "OT Kielce": "świętokrzyskie",
    "OT Koszalin": "zachodniopomorskie",
    "OT Kraków": "małopolskie",
    "OT Lublin": "lubelskie",
    "OT Łódź": "łódzkie",
    "OT Olsztyn": "warmińsko-mazurskie",
    "OT Opole": "opolskie",
    "OT Poznań": "wielkopolskie",
    "OT Pruszcz Gdański": "pomorskie",
    "OT Rzeszów": "podkarpackie",
    "OT Szczecin": "zachodniopomorskie",
    "OT Warszawa": "mazowieckie",
    "OT Wrocław": "dolnośląskie",
}

VOIVODESHIP_ALIASES = {
    "dolnoslask": "dolnośląskie",
    "kujawsko pomorsk": "kujawsko-pomorskie",
    "lubelsk": "lubelskie",
    "lubusk": "lubuskie",
    "lodzk": "łódzkie",
    "malopolsk": "małopolskie",
    "mazowieck": "mazowieckie",
    "opolsk": "opolskie",
    "podkarpack": "podkarpackie",
    "podlask": "podlaskie",
    "pomorsk": "pomorskie",
    "slask": "śląskie",
    "swietokrzysk": "świętokrzyskie",
    "warminsko mazursk": "warmińsko-mazurskie",
    "wielkopolsk": "wielkopolskie",
    "zachodniopomorsk": "zachodniopomorskie",
}

STOPWORDS = {
    "roboty", "budowlane", "budowa", "przebudowa", "remont", "wykonanie", "wykonania",
    "zamowienie", "postepowanie", "krajowy", "osrodek", "wsparcia", "rolnictwa", "oddzial",
    "terenowy", "kowr", "budynku", "budynkow", "obiektu", "obiektow", "wraz", "oraz",
    "terenie", "polozonego", "polozonych", "gmina", "powiat", "wojewodztwo", "czesc", "czesci",
}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00a0", " ").replace("\u200b", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", unidecode(clean_text(value)).lower()).strip()


def parse_money(value: str) -> float | None:
    s = clean_text(value).upper().replace("PLN", "").replace(" ", "")
    s = re.sub(r"[^0-9,.-]", "", s)
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def request_with_retries(url: str, *, timeout: int = 60, binary: bool = False) -> bytes | str:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.content if binary else response.text
        except Exception as exc:
            last_error = exc
            time.sleep(2 + attempt * 3)
    raise RuntimeError(f"Nie udało się pobrać {url}: {last_error}")


def infer_unit_from_title(title: str) -> str:
    text = clean_text(title)
    m = re.search(r"\bOT\s+([A-ZĄĆĘŁŃÓŚŹŻ][^\-–|]+)", text, flags=re.I)
    if m:
        unit = clean_text(m.group(1))
        unit = re.split(r"\b(?:plan|aktualizacja|postep|postęp)\b", unit, flags=re.I)[0].strip(" -–")
        return f"OT {unit}" if unit else ""
    if "centrala" in norm(text):
        return "Centrala"
    return ""


def nearest_unit_heading(anchor: Any) -> str:
    heading = anchor.find_previous(["h2", "h3", "h4", "h5"])
    while heading is not None:
        text = clean_text(heading.get_text(" ", strip=True))
        if text == "Centrala" or re.match(r"^OT\s+", text, flags=re.I):
            return text
        heading = heading.find_previous(["h2", "h3", "h4", "h5"])
    return ""


def fetch_plan_links(year: int) -> list[dict[str, str]]:
    html = request_with_retries(PLAN_PAGE, timeout=75, binary=False)
    soup = BeautifulSoup(html, "html.parser")
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if "/attachment/" not in href:
            continue
        title = clean_text(anchor.get_text(" ", strip=True))
        context = f"{title} {anchor.get('title', '')}"
        if str(year) not in context:
            continue
        url = urljoin(PLAN_PAGE, href)
        if url in seen:
            continue
        unit = nearest_unit_heading(anchor) or infer_unit_from_title(title)
        if not unit:
            unit = "Nieustalona jednostka"
        found.append({"unit": unit, "title": title, "url": url})
        seen.add(url)

    if not found:
        raise RuntimeError(f"Nie znaleziono na stronie KOWR żadnych planów na {year} rok")
    return found


def extract_pdf_text(data: bytes) -> str:
    chunks: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                chunks.append(page.extract_text() or "")
    except Exception:
        pass
    text = "\n".join(chunks).strip()
    if len(text) >= 100 or fitz is None:
        return text
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        text = "\n".join(page.get_text("text") for page in doc)
    except Exception:
        pass
    return text.strip()


def parse_pdf_metadata(data: bytes, fallback_unit: str, title: str, url: str) -> dict[str, Any]:
    text = extract_pdf_text(data)
    first = text[:8000]

    year = None
    m = re.search(r"Plan postępowań.*?na rok\s*([0-9\s]{4,9})", first, flags=re.I | re.S)
    if m:
        digits = re.sub(r"\D", "", m.group(1))[:4]
        if len(digits) == 4:
            year = int(digits)
    if year is None:
        m = re.search(r"\b(20\d{2})\b", title)
        year = int(m.group(1)) if m else CURRENT_YEAR

    version = 0
    m = re.search(r"Wersja\s+nr\s*(\d+)", first, flags=re.I)
    if m:
        version = int(m.group(1))
    if not version:
        m = re.search(r"20\d{2}/BZP\s*[0-9 ]+/(\d{2})/P", first, flags=re.I)
        if m:
            version = int(m.group(1))
    if not version:
        m = re.search(r"(?:wersja|aktualizacja|korekta)\D{0,8}(\d+)", title, flags=re.I)
        if m:
            version = int(m.group(1))

    publication_date = ""
    m = re.search(r"Zamieszczony[^\n]*?w dniu\s*(\d{2}\.\d{2}\.\d{4})", first, flags=re.I)
    if m:
        publication_date = m.group(1)

    bzp_number = ""
    current_header = first.split("\n", 6)[:6]
    current_header_text = " ".join(current_header)
    m = re.search(r"(20\d{2}/BZP\s*[0-9 ]+/\d{2}/P)", current_header_text, flags=re.I)
    if m:
        bzp_number = clean_text(m.group(1))
        vm = re.search(r"/(\d{2})/P$", bzp_number, flags=re.I)
        if vm and version and int(vm.group(1)) != version:
            bzp_number = ""

    city = ""
    m = re.search(r"Miejscowość:\s*(.*?)\s+Kod pocztowy", first, flags=re.I)
    if m:
        city = clean_text(m.group(1))

    unit = fallback_unit
    if unit == "Nieustalona jednostka" and city:
        unit = "Centrala" if norm(city) == "warszawa" else f"OT {city}"

    return {
        "unit": unit,
        "title": title,
        "url": url,
        "year": year,
        "version": version,
        "publication_date": publication_date,
        "bzp_number": bzp_number,
        "city": city,
        "text_length": len(text),
    }


def parse_construction_rows(data: bytes) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            try:
                tables = page.extract_tables() or []
            except Exception:
                tables = []
            for table in tables:
                for row in table or []:
                    if not row:
                        continue
                    cells = [clean_text(x) for x in row]
                    position = cells[0] if cells else ""
                    if not re.fullmatch(r"[12]\.1\.\d+", position):
                        continue
                    if position in seen:
                        continue
                    while len(cells) < 7:
                        cells.append("")
                    subject, procedure, value_text, term, additional, update = cells[1:7]
                    if not subject:
                        continue
                    items.append({
                        "position": position,
                        "subject": subject,
                        "procedure": procedure,
                        "value_text": value_text,
                        "value_net": parse_money(value_text),
                        "planned_start": term,
                        "additional_info": additional,
                        "update_info": update,
                        "threshold": "poniżej progów UE" if position.startswith("1.") else "progi UE lub powyżej",
                        "page": page_no,
                    })
                    seen.add(position)
    return items


def date_sort_key(doc: dict[str, Any]) -> tuple[int, int, str]:
    d = ""
    try:
        d = datetime.strptime(doc.get("publication_date", ""), "%d.%m.%Y").strftime("%Y%m%d")
    except Exception:
        pass
    return int(doc.get("year", 0)), int(doc.get("version", 0)), d


def changes_between(old: dict[str, Any] | None, new: dict[str, Any]) -> tuple[str, str]:
    update = norm(new.get("update_info", ""))
    if "rezygn" in update:
        return "REZYGNACJA", clean_text(new.get("update_info", "")) or "Rezygnacja z pozycji"
    if "dodana" in update or "dodano" in update:
        return "NOWE", clean_text(new.get("update_info", "")) or "Dodana pozycja"
    if "zmiana" in update or "zmieniono" in update:
        return "ZMIANA", clean_text(new.get("update_info", "")) or "Pozycja zmieniona"
    if old is None:
        return "NOWE", "Pozycja nie występowała w poprzedniej wersji planu"

    diffs: list[str] = []
    for key, label in [
        ("subject", "przedmiot"), ("value_text", "wartość"),
        ("planned_start", "termin"), ("procedure", "tryb"),
    ]:
        if norm(str(old.get(key, ""))) != norm(str(new.get(key, ""))):
            diffs.append(label)
    if diffs:
        return "ZMIANA", "Zmieniono: " + ", ".join(diffs)
    return "BEZ ZMIAN", ""


def parse_planned_range(term: str, year: int) -> tuple[date, date] | None:
    t = norm(term)
    romans = re.findall(r"\b(?:iv|iii|ii|i)\b", t)
    q_values = [ROMAN_QUARTERS.get(x.upper()) for x in romans if ROMAN_QUARTERS.get(x.upper())]
    if "kwart" in t and q_values:
        q1, q2 = min(q_values), max(q_values)
        start_month = (q1 - 1) * 3 + 1
        end_month = q2 * 3
        if end_month == 12:
            end_day = 31
        else:
            end_day = (date(year, end_month + 1, 1) - timedelta(days=1)).day
        return date(year, start_month, 1), date(year, end_month, end_day)

    months = [num for name, num in MONTHS.items() if re.search(rf"\b{name}\b", t)]
    if months:
        m1, m2 = min(months), max(months)
        if m2 == 12:
            end_day = 31
        else:
            end_day = (date(year, m2 + 1, 1) - timedelta(days=1)).day
        return date(year, m1, 1), date(year, m2, end_day)
    return None


def timing_status(term: str, year: int, has_match: bool) -> str:
    if has_match:
        return "OPUBLIKOWANE"
    planned = parse_planned_range(term, year)
    if not planned:
        return "PLANOWANE"
    today = date.today()
    start, end = planned
    if today > end:
        return "PO TERMINIE – BRAK OGŁOSZENIA"
    days_to_start = (start - today).days
    if days_to_start <= 100:
        return "ZBLIŻA SIĘ"
    return "PLANOWANE"


def extract_location(subject: str) -> str:
    text = clean_text(subject)
    m = re.search(r"(?:miejscowościach|miejscowości|msc\.|m\.)\s+(.+)$", text, flags=re.I)
    if m:
        loc = clean_text(m.group(1)).strip(" -–,.")
        return loc[:157].rstrip() + "…" if len(loc) > 160 else loc

    dash_candidates = re.findall(r"-\s*([^.;]+?(?:gmina|gm\.|powiat|woj\.)[^.;]*)", text, flags=re.I)
    if dash_candidates:
        loc = clean_text(dash_candidates[-1]).strip(" -–,.")
        return loc[:157].rstrip() + "…" if len(loc) > 160 else loc

    m = re.search(r"\bw\s+([A-ZĄĆĘŁŃÓŚŹŻ][^.;]+?(?:,\s*(?:gmina|gm\.|powiat|woj\.)[^.;]*)?)$", text)
    if m:
        loc = clean_text(m.group(1)).strip(" -–,.")
        return loc[:157].rstrip() + "…" if len(loc) > 160 else loc

    m = re.search(r"(?:przy ul\.|ul\.)\s+([^.;]+)", text, flags=re.I)
    if m:
        loc = clean_text(m.group(1)).strip(" -–,.")
        return loc[:157].rstrip() + "…" if len(loc) > 160 else loc

    # W planach lokalizacja jest często integralną częścią przedmiotu; nie zgadujemy jej na siłę.
    return ""



def infer_voivodeship(unit: str, *texts: str) -> str:
    """Ustala województwo najpierw z treści planu, a potem z właściwości OT."""
    combined = norm(" ".join(clean_text(text) for text in texts if text))

    # Dłuższe i bardziej charakterystyczne nazwy sprawdzamy jako pierwsze.
    for alias, canonical in sorted(VOIVODESHIP_ALIASES.items(), key=lambda x: len(x[0]), reverse=True):
        if alias in combined:
            return canonical

    return VOIVODESHIP_BY_UNIT.get(clean_text(unit), "Nieustalone")

def significant_tokens(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]{4,}", norm(text)))
    return {w for w in words if w not in STOPWORDS and not w.isdigit()}


def scrape_auction_page(url: str) -> list[dict[str, str]]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT, locale="pl-PL", viewport={"width": 1600, "height": 1000})
        page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(9_000)

        # Próba ustawienia największej liczby rekordów na stronę.
        for idx in range(page.locator("select").count()):
            select = page.locator("select").nth(idx)
            try:
                options = select.locator("option").evaluate_all(
                    "opts => opts.map(o => ({value:o.value, text:(o.textContent||'').trim()}))"
                )
                numeric = []
                for opt in options:
                    m = re.search(r"\b(20|50|100|200|500)\b", opt.get("text", ""))
                    if m:
                        numeric.append((int(m.group(1)), opt.get("value", "")))
                if numeric:
                    select.select_option(max(numeric)[1])
                    page.wait_for_timeout(2_000)
            except Exception:
                continue

        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2_000)
        raw = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('a[href*="open-preview-auction"]')).map(a => ({
              href: a.href,
              text: ((a.closest('tr') && a.closest('tr').innerText) || a.innerText || a.textContent || '').trim()
            }))
            """
        )
        browser.close()

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        href = clean_text(item.get("href", ""))
        if not href or href in seen:
            continue
        out.append({"url": href, "text": clean_text(item.get("text", "")), "source": url})
        seen.add(href)
    return out


def collect_auctions() -> tuple[list[dict[str, str]], list[str]]:
    if os.getenv("SKIP_EB2B") == "1":
        return [], ["Skan eB2B pominięty przez SKIP_EB2B=1"]
    auctions: list[dict[str, str]] = []
    errors: list[str] = []
    for url in (OPEN_AUCTIONS, RESULTS_AUCTIONS):
        try:
            auctions.extend(scrape_auction_page(url))
        except Exception as exc:
            errors.append(f"eB2B {url}: {type(exc).__name__}: {exc}")
    unique = {a["url"]: a for a in auctions}
    return list(unique.values()), errors


def match_auctions(item: dict[str, Any], auctions: list[dict[str, str]]) -> list[dict[str, Any]]:
    subject = item.get("subject", "")
    unit = item.get("unit", "")
    s_tokens = significant_tokens(subject)
    unit_tokens = significant_tokens(unit)
    candidates: list[dict[str, Any]] = []

    for auction in auctions:
        text = auction.get("text", "")
        a_tokens = significant_tokens(text)
        overlap = s_tokens & a_tokens
        score = float(fuzz.token_set_ratio(norm(subject), norm(text)))
        if unit_tokens & a_tokens:
            score += 8
        long_overlap = {x for x in overlap if len(x) >= 6}
        if len(overlap) >= 2:
            score += min(10, len(overlap) * 2)
        elif long_overlap:
            score += 5
        accepted = score >= 72 or (score >= 60 and len(long_overlap) >= 2)
        if accepted:
            candidates.append({
                "url": auction["url"],
                "title": text[:500],
                "confidence": round(min(score, 100), 1),
            })

    candidates.sort(key=lambda x: x["confidence"], reverse=True)
    return candidates[:5]


def choose_primary_status(change_status: str, time_status: str) -> str:
    if change_status == "REZYGNACJA":
        return "REZYGNACJA"
    if time_status == "OPUBLIKOWANE":
        return "OPUBLIKOWANE"
    if change_status in {"NOWE", "ZMIANA"}:
        return change_status
    if time_status in {"PO TERMINIE – BRAK OGŁOSZENIA", "ZBLIŻA SIĘ"}:
        return time_status
    return "PLANOWANE"


def run() -> dict[str, Any]:
    DATA_DIR.mkdir(exist_ok=True)
    DOCS_DIR.mkdir(exist_ok=True)
    state = load_json(STATE_FILE, {"matches": {}, "known_documents": {}})
    errors: list[str] = []

    links = fetch_plan_links(CURRENT_YEAR)
    documents: list[dict[str, Any]] = []
    for idx, link in enumerate(links, start=1):
        try:
            print(f"[{idx}/{len(links)}] {link['unit']}: {link['title'][:90]}")
            pdf_data = request_with_retries(link["url"], timeout=90, binary=True)
            assert isinstance(pdf_data, bytes)
            meta = parse_pdf_metadata(pdf_data, link["unit"], link["title"], link["url"])
            if int(meta.get("year", 0)) != CURRENT_YEAR:
                continue
            rows = parse_construction_rows(pdf_data)
            meta["items"] = rows
            documents.append(meta)
        except Exception as exc:
            errors.append(f"{link['unit']} | {link['title']}: {type(exc).__name__}: {exc}")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for doc in documents:
        grouped[doc["unit"]].append(doc)

    auctions, auction_errors = collect_auctions()
    errors.extend(auction_errors)

    result_items: list[dict[str, Any]] = []
    for unit, docs in grouped.items():
        docs.sort(key=date_sort_key, reverse=True)
        latest = docs[0]
        previous = docs[1] if len(docs) > 1 else None
        previous_by_pos = {x["position"]: x for x in (previous or {}).get("items", [])}

        for row in latest.get("items", []):
            item_id = f"{unit}|{CURRENT_YEAR}|{row['position']}"
            old = previous_by_pos.get(row["position"])
            change_status, change_detail = changes_between(old, row)
            item = {
                **row,
                "id": item_id,
                "unit": unit,
                "year": CURRENT_YEAR,
                "plan_version": latest.get("version", 0),
                "plan_date": latest.get("publication_date", ""),
                "plan_bzp": latest.get("bzp_number", ""),
                "plan_title": latest.get("title", ""),
                "plan_url": latest.get("url", PLAN_PAGE),
                "previous_version": previous.get("version", 0) if previous else None,
                "location": extract_location(row.get("subject", "")),
                "voivodeship": infer_voivodeship(
                    unit,
                    row.get("subject", ""),
                    row.get("additional_info", ""),
                    latest.get("city", ""),
                ),
                "change_status": change_status,
                "change_detail": change_detail,
            }

            current_matches = match_auctions(item, auctions)
            preserved = state.get("matches", {}).get(item_id, [])
            merged: dict[str, dict[str, Any]] = {}
            for match in [*preserved, *current_matches]:
                if match.get("url"):
                    merged[match["url"]] = match
            item["announcement_matches"] = sorted(
                merged.values(), key=lambda x: float(x.get("confidence", 0)), reverse=True
            )[:8]
            item["announcement_url"] = item["announcement_matches"][0]["url"] if item["announcement_matches"] else ""
            item["timing_status"] = timing_status(
                item.get("planned_start", ""), CURRENT_YEAR, bool(item["announcement_matches"])
            )
            item["status"] = choose_primary_status(change_status, item["timing_status"])
            result_items.append(item)
            state.setdefault("matches", {})[item_id] = item["announcement_matches"]

    priority = {
        "NOWE": 0, "ZMIANA": 1, "PO TERMINIE – BRAK OGŁOSZENIA": 2,
        "ZBLIŻA SIĘ": 3, "OPUBLIKOWANE": 4, "PLANOWANE": 5, "REZYGNACJA": 6,
    }
    result_items.sort(key=lambda x: (priority.get(x["status"], 9), -(x.get("value_net") or 0), x["unit"]))

    active = [x for x in result_items if x["status"] != "REZYGNACJA"]
    present_units = set(grouped)
    missing_units = [u for u in EXPECTED_UNITS if u not in present_units]
    summary = {
        "items": len(active),
        "units": len(grouped),
        "documents": len(documents),
        "value_net": round(sum(x.get("value_net") or 0 for x in active), 2),
        "new_or_changed": sum(x["change_status"] in {"NOWE", "ZMIANA"} for x in active),
        "published": sum(bool(x.get("announcement_matches")) for x in active),
        "past_due": sum(x["timing_status"] == "PO TERMINIE – BRAK OGŁOSZENIA" for x in active),
        "expected_units": len(EXPECTED_UNITS),
        "missing_units": len(missing_units),
    }

    payload = {
        "meta": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "year": CURRENT_YEAR,
            "plan_page": PLAN_PAGE,
            "platform_url": OPEN_AUCTIONS,
            "mode": "live",
            "summary": summary,
            "errors": errors,
            "missing_units": missing_units,
        },
        "items": result_items,
        "documents": [
            {k: v for k, v in doc.items() if k != "items"} | {"construction_items": len(doc.get("items", []))}
            for doc in documents
        ],
    }

    state["last_run"] = payload["meta"]["generated_at"]
    state["known_documents"] = {doc["url"]: date_sort_key(doc) for doc in documents}
    save_json(STATE_FILE, state)
    save_json(OUTPUT_FILE, payload)
    save_json(WEB_OUTPUT_FILE, payload)
    return payload


if __name__ == "__main__":
    try:
        output = run()
        print(json.dumps(output["meta"]["summary"], ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"BŁĄD KRYTYCZNY: {type(exc).__name__}: {exc}", file=sys.stderr)
        # Nie kasujemy poprzednich wyników, gdy chwilowo nie działa źródło.
        sys.exit(1)
