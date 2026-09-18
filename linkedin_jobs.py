#!/usr/bin/env python3
"""Sort LinkedIn job searches into fit, stretch, and data-harvest postings.

LinkedIn does not give a personal export of your searches, and signing in
from a script can get the account restricted. This reads searches you already
opened: a page you saved, or jobs you pasted into inbox/.

    python linkedin_jobs.py
    python linkedin_jobs.py --demo
    python linkedin_jobs.py path\\to\\saved-search.html

Save a search: open it while logged in, scroll the list, then
File, Save Page As, Webpage, Complete. Drop the .html or .htm file in inbox/.
The saved page is titled after the job LinkedIn already opened on the right.
That does not replace the list. That open job is the only one with a full
description; the rest of the list is judged from the title, company, and location.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent
INBOX = ROOT / "inbox"
EXAMPLES = ROOT / "examples" / "sample-jobs.txt"
PROFILE_PATH = ROOT / "profile.json"
OUTPUT_DIR = ROOT / "output"

APPLICABLE = "applicable"
STRETCH = "stretch"
UNRELIABLE = "unreliable"
UNINTERESTED = "uninterested"
UNRELATED = "unrelated"

# One of these is enough. They mean the posting is collecting a profile,
# not offering a concrete job you can start.
STRONG_HARVEST = [
    (r"spontaneous application|candidatura spontanea", "it is a spontaneous application, not a named opening"),
    (r"talent community|talent pool|talent network", "it is a talent pool, not one job"),
    (r"not a specific (?:opening|role|position|job)", "it says there is no specific opening"),
    (r"when a (?:role|position|job) (?:in this area )?becomes available|as soon as a role .{0,40}becomes available", "they will contact you only if a role opens later"),
    (r"future (?:roles|openings|opportunities)", "it is collecting interest in future roles"),
    (r"create (?:a |your )?(?:profile|account) to (?:apply|see|view|get matched)|sign up to (?:see|view|apply)", "you have to join their site before you can see the employer"),
    (r"get matched with (?:employers|opportunities|jobs|companies)|we (?:will |'ll )?(?:match|pair) you", "the offer is to match you, not to hire you"),
    (r"join our platform|register your interest|expression of interest", "it asks you to register interest instead of applying to a role"),
]

# Need two of these, unless the company is already a known board.
WEAK_HARVEST = [
    r"constantly looking for .{0,40}(?:students|graduates|juniors|professionals)",
    r"our community of (?:over )?(?:\d{1,3}(?:,\d{3})+|\d+\s*million)",
    r"\d(?:,\d{3}){2,}\s+(?:users|members|candidates)",
    r"upload your (?:cv|resume|curriculum)",
    r"keep your (?:cv|resume) on file",
    r"evergreen (?:role|posting|requisition)",
    r"multiple (?:clients|companies|employers)",
    r"confidential (?:company|client|employer)",
    r"various companies",
    r"we are hiring for many",
]

SENIOR_TITLE = re.compile(
    r"\b(?:senior|sr\.?|staff|principal|director|head of|vice president|"
    r"vp of|chief|architect|founding|member of technical staff)\b"
    r"|\b(?:lead|manager)\b(?!\s+(?:generation|magnet))",
    re.I,
)
ENTRY_TITLE = re.compile(
    r"\b(?:junior|jr\.?|entry[-\s]?level|new grad(?:uate)?|associate|"
    r"intern(?:ship)?|apprentice|graduate program)\b",
    re.I,
)
YEAR_SPAN = re.compile(
    r"(?<!age[d ] )(\d+)\s*(?:\+|plus)?\s*(?:-|–|to)\s*(\d+)\s*\+?\s*years?(?![\w-]*\s*old)",
    re.I,
)
YEAR_MIN = re.compile(
    r"(?:(\d+)\s*\+\s*years?|"
    r"(?:minimum of |at least |minimum )?(\d+)\s*\+\s*years?(?: of)? experience|"
    r"(?:minimum of |at least )(\d+)\s*years?)",
    re.I,
)


@dataclass
class Listing:
    title: str
    company: str = ""
    location: str = ""
    description: str = ""
    url: str = ""
    source: str = ""
    search_query: str = ""


@dataclass
class Judgment:
    listing: Listing
    bucket: str
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def load_profile() -> dict:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def contains_term(text: str, term: str) -> bool:
    term = term.strip().lower()
    if not term or term in {"c", "r"}:
        return False
    pattern = re.escape(term).replace(r"\ ", r"\s+")
    return re.search(rf"(?<![a-z0-9+#]){pattern}(?![a-z0-9+#])", text, re.I) is not None


def blob(listing: Listing) -> str:
    return " ".join(
        part for part in (listing.title, listing.company, listing.location, listing.description) if part
    )


def company_is_board(listing: Listing, profile: dict) -> str | None:
    company = listing.company.lower()
    url = listing.url.lower()
    haystack = f"{company} {url}"
    for name in profile.get("harvest_companies", []):
        if contains_term(haystack, name) or name.lower() in company:
            return name
    return None


EMAIL = r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}"
# Duties that sound like a real role until you notice they never say what the company does.
VAGUE_DUTIES = [
    r"own the full stack",
    r"lead product direction",
    r"applied ai effectively",
    r"iterate fast",
    r"ship with (?:good )?velocity",
    r"ambiguous situation",
    r"any level of experience",
    r"good product intuition",
    r"stack agnostic",
]
COMPANY_PITCH = re.compile(
    r"\b(?:we (?:build|make|publish|power|help|design)|"
    r"our (?:platform|product|mission|customers|purpose)|company overview)\b",
    re.I,
)


def harvest_reasons(listing: Listing, profile: dict) -> list[str]:
    text = blob(listing)
    reasons: list[str] = []
    board = company_is_board(listing, profile)
    if board:
        reasons.append(f"company looks like a job board ({board}), not the employer")
    for pattern, reason in STRONG_HARVEST:
        if re.search(pattern, text, re.I):
            reasons.append(reason)
    weak = [pattern for pattern in WEAK_HARVEST if re.search(pattern, text, re.I)]
    if len(weak) >= 2:
        reasons.append("reads like a talent-pool ad rather than one role")
    elif board and weak:
        reasons.append("job-board company plus talent-pool language")
    contact = apply_by_contact(listing.description)
    if contact:
        reasons.append(contact)
    ramp = ramp_plan(listing.description)
    if ramp:
        reasons.append(ramp)
    vague = vague_posting(listing)
    if vague:
        reasons.append(vague)
    return reasons


def apply_by_contact(description: str) -> str | None:
    """The posting tells you to email, call, or message instead of applying on the job."""
    if not description:
        return None
    text = re.sub(
        r"do not (?:send|email|reply).{0,80}|"
        r"(?:accommodation|accessibility|privacy notice).{0,120}|"
        r"official communication.{0,160}|"
        r"will come (?:exclusively )?from email",
        " ",
        description,
        flags=re.I,
    )
    if re.search(
        rf"send (?:your |a )?(?:resume|cv|introduction|application).{{0,140}}(?:via|to|at)\s+{EMAIL}",
        text,
        re.I,
    ) or re.search(rf"(?:resume|cv).{{0,50}}(?:to|via)\s+{EMAIL}", text, re.I):
        return "asks you to email a resume instead of applying on the posting"
    if re.search(
        r"\b(?:reach out|apply|contact (?:us|me)) (?:to us )?(?:by |via |through |at )"
        r"(?:email|e-mail|phone|text|whatsapp|telegram)\b",
        text,
        re.I,
    ):
        return "asks you to reach out directly instead of applying on the posting"
    if re.search(r"\b(?:call|text|whatsapp|telegram) (?:us |me )?at\b.{0,40}\d{3}", text, re.I):
        return "asks you to call or message instead of applying on the posting"
    return None


def ramp_plan(description: str) -> str | None:
    if not description:
        return None
    if re.search(r"30\s*[/\-]\s*60\s*[/\-]\s*90", description, re.I):
        return "uses a 30/60/90 day plan instead of a settled role"
    days = {
        mark
        for mark in ("30", "60", "90")
        if re.search(rf"\b(?:first |by |within (?:the )?|after )?{mark} days\b", description, re.I)
    }
    if "30" in days and "90" in days:
        return "lays out a 30, 60, and 90 day plan instead of a settled role"
    return None


def vague_posting(listing: Listing) -> str | None:
    description = listing.description or ""
    words = re.findall(r"[A-Za-z']+", description)
    if len(words) < 40 or len(words) > 550:
        return None
    generic = sum(1 for pattern in VAGUE_DUTIES if re.search(pattern, description, re.I))
    if generic >= 3 and not COMPANY_PITCH.search(description):
        return "description is too vague to tell what the job actually is"
    return None


def preference_reasons(listing: Listing, profile: dict) -> list[str]:
    text = blob(listing)
    text = re.sub(
        r"equal opportunity.{0,500}|without regard to.{0,280}|"
        r"does not discriminate.{0,320}|military status|veteran status",
        " ",
        text,
        flags=re.I,
    )
    reasons: list[str] = []
    for topic in profile.get("uninterested_topics", []):
        label = str(topic.get("label", "outside preference"))
        hits = [term for term in topic.get("terms", []) if contains_term(text, term)]
        if hits:
            reasons.append(f"{label}: " + ", ".join(hits[:3]))
    return reasons


def required_years(text: str) -> int | None:
    cleaned = re.sub(r"\d+\s*years?\s+old", " ", text, flags=re.I)
    found: list[int] = []
    for match in YEAR_SPAN.finditer(cleaned):
        found.append(int(match.group(1)))
    for match in YEAR_MIN.finditer(cleaned):
        number = next(group for group in match.groups() if group)
        found.append(int(number))
    return max(found) if found else None


def skill_hits(listing: Listing, profile: dict) -> list[str]:
    text = blob(listing)
    hits: list[str] = []
    for term in profile.get("skills", []):
        if contains_term(text, term) and term.lower() not in {hit.lower() for hit in hits}:
            hits.append(term)
    return hits


def field_hits(listing: Listing, profile: dict) -> list[str]:
    title = listing.title.lower()
    text = blob(listing).lower()
    hits: list[str] = []
    for term in profile.get("fields", []):
        if contains_term(title, term) or contains_term(text, term):
            if term.lower() not in {hit.lower() for hit in hits}:
                hits.append(term)
    return hits


def title_fit(listing: Listing, profile: dict) -> str | None:
    title = listing.title.lower()
    for term in sorted(profile.get("title_fits", []), key=len, reverse=True):
        if contains_term(title, term):
            return term
    return None


def location_note(listing: Listing, profile: dict) -> str | None:
    place = f" {listing.location.lower()} "
    if not listing.location:
        return None
    if any(hint.strip() and hint.lower() in place or hint.lower() in f" {listing.description.lower()} " for hint in ["remote"]):
        if "remote" in place or re.search(r"\bremote\b", listing.location, re.I):
            return "remote"
    nearby = profile.get("nearby", [])
    padded = f" {place} "
    if any(hint.lower() in padded for hint in nearby if hint.strip()):
        return "near home"
    if re.search(r"\bon[- ]?site\b|\bin[- ]office\b|\bin office\b", listing.location, re.I):
        return "on-site outside your usual area"
    return "location not obviously near Canton"


def judge(listing: Listing, profile: dict) -> Judgment:
    harvest = harvest_reasons(listing, profile)
    if harvest:
        return Judgment(listing, UNRELIABLE, _unique(harvest[:4]))

    aside = preference_reasons(listing, profile)
    if aside:
        return Judgment(listing, UNINTERESTED, _unique(aside[:3]))

    years = required_years(blob(listing))
    skills = skill_hits(listing, profile)
    fields = field_hits(listing, profile)
    title = title_fit(listing, profile)
    in_wheelhouse = bool(title or skills or _title_field(listing, fields))
    notes: list[str] = []
    place = location_note(listing, profile)
    if place:
        notes.append(place)

    if not in_wheelhouse:
        return Judgment(
            listing,
            UNRELATED,
            ["no overlap with games, software, teaching, writing, or support"],
            notes,
        )

    reasons: list[str] = []
    if title:
        reasons.append(f"title fits ({title})")
    if skills:
        reasons.append("skills: " + ", ".join(skills[:5]))
    elif fields:
        reasons.append("field: " + ", ".join(fields[:4]))

    comfortable = int(profile.get("years_comfortable", 4))
    senior = bool(SENIOR_TITLE.search(listing.title))
    entry = bool(ENTRY_TITLE.search(listing.title))
    stretch_reasons: list[str] = []
    if senior and not entry:
        stretch_reasons.append("title is above a junior or general role")
    if years is not None and years > comfortable:
        stretch_reasons.append(f"asks for {years}+ years (comfortable line is {comfortable})")

    if stretch_reasons:
        return Judgment(listing, STRETCH, reasons + stretch_reasons, notes)
    if entry:
        reasons.append("posted as junior, entry, or intern")
    elif years is not None:
        reasons.append(f"experience ask ({years} years) is in range")
    else:
        reasons.append("no senior title and no high year requirement")
    return Judgment(listing, APPLICABLE, reasons, notes)


def _title_field(listing: Listing, fields: list[str]) -> bool:
    title = listing.title.lower()
    return any(contains_term(title, term) for term in fields)


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        kept.append(item)
    return kept


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1
        if tag in {"p", "div", "li", "br", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts))


def html_to_text(raw: str) -> str:
    parser = _TextExtractor()
    parser.feed(raw)
    return parser.text()


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip(" -|")


def parse_linkedin_html(raw: str, source: str) -> list[Listing]:
    # LinkedIn names the saved file after whichever result is open on the right.
    # The search list is still in the page, under job-card-component-ref-*.
    search_cards = _parse_search_cards(raw, source)
    if search_cards:
        return search_cards

    listings: list[Listing] = []
    seen: set[str] = set()

    for match in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        raw,
        re.I | re.S,
    ):
        listings.extend(_from_jsonld(match.group(1), source, seen))

    card = re.compile(
        r'<a[^>]+href="([^"]*?/jobs/view/[^"]+)"[^>]*?(?:aria-label="([^"]+)")?[^>]*>',
        re.I,
    )
    for match in card.finditer(raw):
        url = html.unescape(match.group(1))
        label = _clean(match.group(2) or "")
        if not label:
            continue
        title, company = _split_label(label)
        key = url.split("?")[0]
        if key in seen or not title:
            continue
        seen.add(key)
        listings.append(Listing(title=title, company=company, url=_abs(url), source=source))

    if listings:
        return listings

    title = _first_tag(raw, "h1")
    if title and "/jobs/view/" in raw:
        company = _first_class(raw, "job-details-jobs-unified-top-card__company-name")
        location = _first_class(raw, "job-details-jobs-unified-top-card__bullet")
        description = _first_class(raw, "show-more-less-html__markup")
        url_match = re.search(r"https?://[^\"']+/jobs/view/\d+", raw)
        listings.append(
            Listing(
                title=title,
                company=company,
                location=location,
                description=description or _trim(html_to_text(raw)),
                url=url_match.group(0) if url_match else "",
                source=source,
            )
        )
    return listings


def _parse_search_cards(raw: str, source: str) -> list[Listing]:
    if "job-card-component-ref-" not in raw:
        return []
    current = ""
    current_match = re.search(r"currentJobId=(\d+)", raw)
    if current_match:
        current = current_match.group(1)
    query = ""
    query_match = re.search(r"keywords=([^&\"'\s]+)", raw)
    if query_match:
        query = _clean(unquote(html.unescape(query_match.group(1)).replace("+", " ")))
    description = _about_the_job(raw) if current else ""

    first_at: dict[str, int] = {}
    for match in re.finditer(r'componentkey="job-card-component-ref-(\d+)"', raw):
        first_at.setdefault(match.group(1), match.start())
    ordered = sorted(first_at.items(), key=lambda item: item[1])
    listings: list[Listing] = []
    for index, (job_id, start) in enumerate(ordered):
        end = ordered[index + 1][1] if index + 1 < len(ordered) else start + 12000
        title, company, location = _card_fields(_visible_lines(raw[start:end]))
        if not title:
            continue
        listings.append(
            Listing(
                title=title,
                company=company,
                location=location,
                description=description if job_id == current else "",
                url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                source=source,
                search_query=query,
            )
        )
    return listings


def _visible_lines(chunk: str) -> list[str]:
    chunk = re.sub(r"<script[\s\S]*?</script>", " ", chunk, flags=re.I)
    chunk = re.sub(r"<[^>]+>", "\n", chunk)
    lines: list[str] = []
    skip = re.compile(
        r"^(easy apply|promoted|applied|viewed|see who .+ hired)$",
        re.I,
    )
    for line in html.unescape(chunk).splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        line = re.sub(r"\s*\(Verified job\)\s*$", "", line, flags=re.I)
        if not line or line.startswith("<") or "componentkey=" in line:
            continue
        if skip.match(line) or re.match(r"posted\b|\d+\s+(?:school )?alumni", line, re.I):
            continue
        if lines and (lines[-1] == line or lines[-1].startswith(line)):
            lines[-1] = line
            continue
        if lines and line.startswith(lines[-1]):
            continue
        lines.append(line)
    return lines


def _card_fields(lines: list[str]) -> tuple[str, str, str]:
    if not lines:
        return "", "", ""
    title = lines[0]
    company = lines[1] if len(lines) > 1 else ""
    location = ""
    for line in lines[2:6]:
        if re.search(r"\bago\b|applicant", line, re.I):
            continue
        location = line
        break
    return title, company, location


def _about_the_job(raw: str) -> str:
    start = raw.find("About the job")
    if start < 0:
        return ""
    chunk = raw[start : start + 24000]
    stop = re.search(
        r"About the company|People also viewed|Similar jobs|Meet the hiring team|"
        r"Job poster|More jobs",
        chunk,
    )
    if stop and stop.start() > 80:
        chunk = chunk[: stop.start()]
    text = html.unescape(re.sub(r"<[^>]+>", " ", chunk))
    text = _trim(text)
    return re.sub(r"^About the job\s*", "", text, count=1, flags=re.I).strip()


def _from_jsonld(raw_json: str, source: str, seen: set[str]) -> list[Listing]:
    try:
        data = json.loads(html.unescape(raw_json).strip())
    except json.JSONDecodeError:
        return []
    nodes = data if isinstance(data, list) else [data]
    found: list[Listing] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        kind = str(node.get("@type", ""))
        if "JobPosting" not in kind and node.get("title") is None:
            continue
        if "JobPosting" not in kind and "hiringOrganization" not in node:
            continue
        title = _clean(str(node.get("title", "")))
        org = node.get("hiringOrganization") or {}
        company = _clean(org.get("name", "") if isinstance(org, dict) else str(org))
        location = ""
        job_location = node.get("jobLocation")
        if isinstance(job_location, dict):
            address = job_location.get("address") or {}
            if isinstance(address, dict):
                location = _clean(
                    ", ".join(
                        str(address.get(key, ""))
                        for key in ("addressLocality", "addressRegion")
                        if address.get(key)
                    )
                )
        url = _clean(str(node.get("url", "")))
        description = _trim(re.sub(r"<[^>]+>", " ", str(node.get("description", ""))))
        key = url or f"{title}|{company}"
        if not title or key in seen:
            continue
        seen.add(key)
        found.append(
            Listing(title=title, company=company, location=location, description=description, url=url, source=source)
        )
    return found


def _split_label(label: str) -> tuple[str, str]:
    match = re.match(r"(.+?)\s+at\s+(.+?)(?:\s+with\s+.+)?$", label, re.I)
    if match:
        return _clean(match.group(1)), _clean(match.group(2))
    return label, ""


def _abs(url: str) -> str:
    if url.startswith("/"):
        return "https://www.linkedin.com" + url.split("?")[0]
    return url.split("?")[0]


def _first_tag(raw: str, tag: str) -> str:
    match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", raw, re.I | re.S)
    if not match:
        return ""
    return _clean(re.sub(r"<[^>]+>", " ", match.group(1)))


def _first_class(raw: str, class_name: str) -> str:
    match = re.search(
        rf'class="[^"]*{re.escape(class_name)}[^"]*"[^>]*>(.*?)</',
        raw,
        re.I | re.S,
    )
    if not match:
        return ""
    return _clean(re.sub(r"<[^>]+>", " ", match.group(1)))


def _trim(text: str, limit: int = 12000) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def parse_text_file(raw: str, source: str) -> list[Listing]:
    if raw.lstrip().startswith("{") or raw.lstrip().startswith("["):
        return parse_json_jobs(raw, source)
    blocks = re.split(r"(?m)^---\s*$", raw)
    listings: list[Listing] = []
    for block in blocks:
        listing = _parse_block(block, source)
        if listing:
            listings.append(listing)
    if listings:
        return listings
    return _parse_loose(raw, source)


def _parse_block(block: str, source: str) -> Listing | None:
    body = "\n".join(
        line for line in block.splitlines() if line.strip() and not line.strip().startswith("#")
    ).strip()
    if not body or "title:" not in body.lower() and not re.search(r"(?m)^[A-Z].{4,}$", body):
        return None
    fields: dict[str, str] = {}
    current = ""
    lines: list[str] = []
    for line in body.splitlines():
        match = re.match(r"^(title|company|location|url|description)\s*:\s*(.*)$", line, re.I)
        if match:
            if current:
                fields[current] = "\n".join(lines).strip()
            current = match.group(1).lower()
            lines = [match.group(2)]
        elif current:
            lines.append(line)
    if current:
        fields[current] = "\n".join(lines).strip()
    title = _clean(fields.get("title", ""))
    if not title:
        return None
    return Listing(
        title=title,
        company=_clean(fields.get("company", "")),
        location=_clean(fields.get("location", "")),
        description=_trim(fields.get("description", "")),
        url=_clean(fields.get("url", "")),
        source=source,
    )


def _parse_loose(raw: str, source: str) -> list[Listing]:
    listings: list[Listing] = []
    for url in re.findall(r"https?://(?:www\.)?linkedin\.com/jobs/view/\d+[^\s]*", raw):
        listings.append(Listing(title="LinkedIn job", url=url.split("?")[0], source=source))
    return listings


def parse_json_jobs(raw: str, source: str) -> list[Listing]:
    data = json.loads(raw)
    rows = data if isinstance(data, list) else data.get("jobs", [])
    listings: list[Listing] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("title"):
            continue
        query = ""
        if isinstance(data, dict):
            query = _clean(str(data.get("search", "") or row.get("search", "")))
        listings.append(
            Listing(
                title=_clean(str(row.get("title", ""))),
                company=_clean(str(row.get("company", ""))),
                location=_clean(str(row.get("location", ""))),
                description=_trim(str(row.get("description", ""))),
                url=_clean(str(row.get("url", ""))),
                source=source,
                search_query=query,
            )
        )
    return listings


def load_path(path: Path) -> list[Listing]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in {".html", ".htm"}:
        found = parse_linkedin_html(raw, path.name)
        if found:
            return found
        return parse_text_file(html_to_text(raw), path.name)
    return parse_text_file(raw, path.name)


def collect_inputs(paths: list[Path], demo: bool) -> tuple[list[Listing], bool]:
    if demo:
        return load_path(EXAMPLES), True
    files = list(paths)
    if not files and INBOX.exists():
        files = [
            path
            for path in sorted(INBOX.iterdir())
            if path.is_file()
            and path.suffix.lower() in {".html", ".htm", ".txt", ".json", ".jsonl"}
            and not path.name.startswith((".", "_"))
        ]
    if not files:
        return load_path(EXAMPLES), True
    listings: list[Listing] = []
    for path in files:
        found = load_path(path)
        if not found:
            print(f"No jobs found in {path.name}. If this is a LinkedIn page, open a job and save that page too.")
        listings.extend(found)
    return listings, False


def listing_key(listing: Listing) -> str:
    match = re.search(r"/jobs/view/(\d+)", listing.url or "")
    if match:
        return match.group(1)
    return (listing.url or f"{listing.title}|{listing.company}").lower()


def prefer_description(current: Listing, incoming: Listing) -> Listing:
    if len(incoming.description) <= len(current.description):
        if incoming.search_query and not current.search_query:
            current.search_query = incoming.search_query
        return current
    if current.search_query and not incoming.search_query:
        incoming.search_query = current.search_query
    return incoming


def sort_jobs(listings: list[Listing], profile: dict) -> list[Judgment]:
    chosen: dict[str, Listing] = {}
    order: list[str] = []
    for listing in listings:
        key = listing_key(listing)
        if key not in chosen:
            chosen[key] = listing
            order.append(key)
            continue
        chosen[key] = prefer_description(chosen[key], listing)
    results: list[Judgment] = []
    for key in order:
        results.append(judge(chosen[key], profile))
    order = {APPLICABLE: 0, STRETCH: 1, UNINTERESTED: 2, UNRELIABLE: 3, UNRELATED: 4}
    results.sort(key=lambda item: (order[item.bucket], item.listing.company.lower(), item.listing.title.lower()))
    return results


def render_html(results: list[Judgment], demo: bool) -> str:
    generated = datetime.now().strftime("%B %d, %Y at %I:%M %p").replace(" 0", " ")
    groups = {
        APPLICABLE: [item for item in results if item.bucket == APPLICABLE],
        STRETCH: [item for item in results if item.bucket == STRETCH],
        UNINTERESTED: [item for item in results if item.bucket == UNINTERESTED],
        UNRELIABLE: [item for item in results if item.bucket == UNRELIABLE],
        UNRELATED: [item for item in results if item.bucket == UNRELATED],
    }
    queries = _unique(
        item.listing.search_query for item in results if item.listing.search_query
    )
    search_line = ""
    if queries:
        search_line = "Search: " + " · ".join(queries) + ". "
    banner = (
        "These are examples, so you can see the three buckets. Drop a saved LinkedIn search into inbox/ and run this again."
        if demo
        else search_line
        + "The page title is the job LinkedIn had open. The list was still read. "
        + (
            "Some jobs have no description yet. Open collect-descriptions.html, paste the collector on this search, and drop the downloaded file in inbox."
            if any(not item.listing.description for item in results)
            else "Descriptions were included, so the sort used the posting text, not just the title."
        )
    )

    def cards(items: list[Judgment]) -> str:
        if not items:
            return "<p class='empty'>None in this batch.</p>"
        blocks = []
        for item in items:
            job = item.listing
            bits = [html.escape(part) for part in (job.company, job.location) if part]
            meta = " · ".join(bits) if bits else "Company not listed"
            why = "".join(f"<li>{html.escape(reason)}</li>" for reason in item.reasons)
            notes = ""
            if item.notes:
                notes = "<p class='meta'>" + " · ".join(html.escape(note) for note in item.notes) + "</p>"
            excerpt = ""
            if job.description:
                excerpt = f"<p class='excerpt'>{html.escape(job.description[:500])}</p>"
            link = ""
            if job.url:
                link = f"<p class='links'><a href='{html.escape(job.url)}'>Open posting</a></p>"
            blocks.append(
                f"""
                <article>
                  <h2>{html.escape(job.title)}</h2>
                  <p class='meta'>{meta}</p>
                  {("<p class='meta'>From search: " + html.escape(job.search_query) + "</p>") if job.search_query else ""}
                  {notes}
                  <ul>{why}</ul>
                  {excerpt}
                  {link}
                </article>
                """
            )
        return "\n".join(blocks)

    unrelated = groups[UNRELATED]
    leftover = ""
    if unrelated:
        leftover = f"""
        <details>
          <summary>Left out as unrelated ({len(unrelated)})</summary>
          {cards(unrelated)}
        </details>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LinkedIn job sort</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #1c1917;
      --muted: #57534e;
      --line: #e7e5e4;
      --paper: #fafaf9;
      --card: #fff;
      --accent: #9a3412;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font: 17px/1.5 Georgia, "Iowan Old Style", "Palatino Linotype", serif;
      color: var(--ink);
      background: var(--paper);
    }}
    main {{ max-width: 760px; margin: 0 auto; padding: 40px 20px 72px; }}
    h1 {{ font-size: 1.8rem; line-height: 1.2; margin: 0 0 8px; }}
    h2 {{ font-size: 1.15rem; line-height: 1.3; margin: 0 0 6px; }}
    h3 {{ font-size: 0.95rem; letter-spacing: 0.04em; text-transform: uppercase; margin: 36px 0 12px; }}
    .lede, .note, .meta, .empty, summary {{ color: var(--muted); }}
    .lede {{ margin: 0 0 8px; }}
    .note {{ font-size: 0.92rem; margin: 0 0 8px; }}
    a {{ color: var(--accent); }}
    article {{
      background: var(--card);
      border: 1px solid var(--line);
      border-left: 4px solid var(--accent);
      border-radius: 10px;
      padding: 16px 18px 12px;
      margin: 0 0 12px;
    }}
    section.stretch article {{ border-left-color: #b45309; }}
    section.aside article {{ border-left-color: #1d4ed8; }}
    section.unreliable article {{ border-left-color: #78716c; }}
    ul {{ margin: 8px 0; padding-left: 1.2rem; }}
    .excerpt {{ margin: 10px 0; }}
    .links {{ margin: 0; }}
    details {{ margin-top: 28px; }}
    summary {{ cursor: pointer; }}
  </style>
</head>
<body>
  <main>
    <h1>Your LinkedIn search, sorted</h1>
    <p class="lede">For Sean Connolly · Canton, CT · compiled {html.escape(generated)}</p>
    <p class="note">{html.escape(banner)}</p>
    <p class="note">Edit profile.json to add a board, a topic you usually skip, or a title that should count. Email-to-apply, a 30/60/90 plan, and a vague posting with no real product go in unreliable even when the title sounds fine.</p>
    <section>
      <h3>Generally applicable ({len(groups[APPLICABLE])})</h3>
      {cards(groups[APPLICABLE])}
    </section>
    <section class="stretch">
      <h3>In your wheelhouse, likely a stretch ({len(groups[STRETCH])})</h3>
      {cards(groups[STRETCH])}
    </section>
    <section class="aside">
      <h3>Outside usual preferences, might still consider ({len(groups[UNINTERESTED])})</h3>
      {cards(groups[UNINTERESTED])}
    </section>
    <section class="unreliable">
      <h3>Unreliable or collecting applicants ({len(groups[UNRELIABLE])})</h3>
      {cards(groups[UNRELIABLE])}
    </section>
    {leftover}
  </main>
</body>
</html>
"""


def write_report(results: list[Judgment], demo: bool) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    path = OUTPUT_DIR / f"linkedin-jobs-{stamp}.html"
    path.write_text(render_html(results, demo), encoding="utf-8")
    (OUTPUT_DIR / "latest.html").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def print_summary(results: list[Judgment], path: Path, demo: bool) -> None:
    labels = {
        APPLICABLE: "FIT    ",
        STRETCH: "STRETCH",
        UNINTERESTED: "ASIDE  ",
        UNRELIABLE: "SKIP   ",
        UNRELATED: "UNRELATED",
    }
    if demo:
        print("Example run. Put saved LinkedIn pages in inbox/ to sort a real search.")
    counts = {
        bucket: sum(item.bucket == bucket for item in results)
        for bucket in (APPLICABLE, STRETCH, UNINTERESTED, UNRELIABLE, UNRELATED)
    }
    print(
        f"{counts[APPLICABLE]} applicable, {counts[STRETCH]} stretch, "
        f"{counts[UNINTERESTED]} might consider, {counts[UNRELIABLE]} unreliable, "
        f"{counts[UNRELATED]} unrelated."
    )
    for item in results:
        if item.bucket == UNRELATED:
            continue
        job = item.listing
        where = job.company or "unknown company"
        print(f"\n[{labels[item.bucket]}] {job.title} — {where}")
        for reason in item.reasons:
            print(f"         {reason}")
    print(f"\nSaved {path}")


def self_test(profile: dict) -> int:
    listings = load_path(EXAMPLES)
    results = {item.listing.title: item.bucket for item in sort_jobs(listings, profile)}
    expected = {
        "Junior Unity Developer": APPLICABLE,
        "Computer Skills Tutor": APPLICABLE,
        "Senior Gameplay Programmer": STRETCH,
        "Staff Software Engineer": STRETCH,
        "Junior Software Engineer - Spontaneous Application": UNRELIABLE,
        "Software Developer": UNRELIABLE,
        "Registered Nurse": UNRELATED,
    }
    failed = 0
    for title, bucket in expected.items():
        got = results.get(title)
        if got != bucket:
            print(f"FAIL {title}: expected {bucket}, got {got}")
            failed += 1
        else:
            print(f"ok   {title} -> {bucket}")
    return 1 if failed else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sort saved LinkedIn searches into fit, stretch, and data-harvest postings."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Saved search pages or job text files. Defaults to inbox/.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Sort the built-in examples instead of inbox/.",
    )
    parser.add_argument("--no-open", action="store_true", help="Write the report without opening it.")
    parser.add_argument("--self-test", action="store_true", help="Check the example jobs land in the right buckets.")
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    profile = load_profile()
    if args.self_test:
        return self_test(profile)
    listings, demo = collect_inputs(args.paths, args.demo)
    if not listings:
        print("No jobs to sort. Save a LinkedIn search into inbox/, or run with --demo.")
        return 1
    results = sort_jobs(listings, profile)
    path = write_report(results, demo)
    print_summary(results, path, demo)
    if not args.no_open:
        webbrowser.open(path.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
