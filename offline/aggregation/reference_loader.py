"""Deterministic loader for the offline manual DOCX reference (S9 only)."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ElementTree
import zipfile


REFERENCE_CONTRACT_VERSION = "offline-manual-reference/v1"
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS = {"w": _WORD_NS, "r": _OFFICE_REL_NS, "pr": _REL_NS}
_URL = re.compile(r"https?://[^\s<>]+")
_SECTION = re.compile(r"^(?P<test_id>[1-9]|10)\)\s*(?P<url>https://disk\.yandex\.ru/\S+)\s*$", re.IGNORECASE)
_TRACK = re.compile(r"^(?:\d+\.\s*)?(?P<title>.+?)\s+от\s+(?P<artists>.+?)\s*$", re.IGNORECASE)


class ReferenceFormatError(ValueError):
    """The manually supplied DOCX does not have the confirmed S9 structure."""


def _id(kind: str, material: Any) -> str:
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"{kind}_{digest}"


@dataclass(frozen=True)
class ReferenceProvenance:
    document_id: str
    document_name: str
    paragraph_indices: tuple[int, ...]
    source_url: str | None
    shazam_url: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id, "document_name": self.document_name,
            "paragraph_indices": list(self.paragraph_indices), "source_url": self.source_url,
            "shazam_url": self.shazam_url,
        }


@dataclass(frozen=True)
class ReferenceEntry:
    entry_id: str
    test_id: int
    order: int
    title: str
    artists: str
    version_text: str | None
    temporal_start: None = None
    temporal_end: None = None
    raw_value: str = ""
    provenance: ReferenceProvenance | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id, "test_id": self.test_id, "order": self.order,
            "title": self.title, "artists": self.artists, "version_text": self.version_text,
            "temporal_start": self.temporal_start, "temporal_end": self.temporal_end,
            "raw_value": self.raw_value,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }


@dataclass(frozen=True)
class ReferenceTest:
    test_id: int
    source_url: str
    entries: tuple[ReferenceEntry, ...]
    notes: tuple[str, ...]
    excluded: bool = False
    exclusion_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id, "source_url": self.source_url,
            "entries": [item.to_dict() for item in self.entries], "notes": list(self.notes),
            "excluded": self.excluded, "exclusion_reason": self.exclusion_reason,
        }


@dataclass(frozen=True)
class OfflineReferenceDocument:
    document_id: str
    document_name: str
    tests: tuple[ReferenceTest, ...]
    contract_version: str = REFERENCE_CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version, "document_id": self.document_id,
            "document_name": self.document_name, "tests": [item.to_dict() for item in self.tests],
        }


def _paragraphs(path: Path) -> list[tuple[int, str, tuple[str, ...]]]:
    try:
        with zipfile.ZipFile(path) as archive:
            document = ElementTree.fromstring(archive.read("word/document.xml"))
            relationships = ElementTree.fromstring(archive.read("word/_rels/document.xml.rels"))
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        raise ReferenceFormatError(f"cannot read DOCX reference {path.name}") from error
    links = {
        item.attrib["Id"]: item.attrib.get("Target", "")
        for item in relationships
        if item.attrib.get("Type", "").endswith("/hyperlink")
    }
    output = []
    for index, paragraph in enumerate(document.findall(".//w:body/w:p", _NS)):
        text = "".join(paragraph.itertext()).strip()
        relationship_ids = [item.attrib.get(f"{{{_OFFICE_REL_NS}}}id") for item in paragraph.findall(".//w:hyperlink", _NS)]
        urls = tuple(dict.fromkeys([*(_URL.findall(text)), *(links[item] for item in relationship_ids if item in links)]))
        if text or urls:
            output.append((index, text, urls))
    return output


def _plain_text(text: str) -> str:
    return _URL.sub("", text).strip()


def _version_text(title: str) -> str | None:
    brackets = re.findall(r"[\[(]([^\])]+)[\])]", title)
    return " | ".join(brackets) or None


def load_reference_document(path: str | Path) -> OfflineReferenceDocument:
    """Parse only confirmed numbered manual-reference DOCX sections.

    Entries remain in source order and are never deduplicated.  Paragraphs that
    do not satisfy the explicit ``title от artist`` grammar are preserved as
    test notes; they can never become tracks by inference.
    """
    document_path = Path(path)
    try:
        document_bytes = document_path.read_bytes()
    except OSError as error:
        raise ReferenceFormatError(f"cannot read DOCX reference {document_path.name}") from error
    document_id = "reference_document_" + hashlib.sha256(document_bytes).hexdigest()
    sections: dict[int, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None

    def finish_pending() -> None:
        nonlocal pending
        if pending is None or current is None:
            pending = None
            return
        order = len(current["entries"]) + 1
        raw_value = "\n".join(pending["raw_parts"])
        source_url = current["source_url"]
        provenance = ReferenceProvenance(
            document_id=document_id, document_name=document_path.name,
            paragraph_indices=tuple(pending["paragraph_indices"]), source_url=source_url,
            shazam_url=next((url for url in pending["urls"] if "shazam.com" in url.casefold()), None),
        )
        entry = ReferenceEntry(
            entry_id=_id("reference_entry", {"document": document_id, "test": current["test_id"], "order": order, "raw": raw_value}),
            test_id=current["test_id"], order=order, title=pending["title"], artists=pending["artists"],
            version_text=_version_text(pending["title"]), raw_value=raw_value, provenance=provenance,
        )
        current["entries"].append(entry)
        pending = None

    for paragraph_index, text, urls in _paragraphs(document_path):
        plain = _plain_text(text)
        section = _SECTION.match(text)
        if section:
            finish_pending()
            test_id, source_url = int(section.group("test_id")), section.group("url")
            if test_id in sections:
                raise ReferenceFormatError(f"duplicate reference section {test_id}")
            current = {"test_id": test_id, "source_url": source_url, "entries": [], "notes": []}
            sections[test_id] = current
            continue
        if current is None:
            if plain:
                raise ReferenceFormatError("reference text appears before first numbered section")
            continue
        track = _TRACK.match(plain)
        if track:
            finish_pending()
            pending = {
                "title": track.group("title").strip(), "artists": track.group("artists").strip(),
                "raw_parts": [text], "paragraph_indices": [paragraph_index], "urls": list(urls),
            }
            if any("shazam.com" in url.casefold() for url in urls):
                finish_pending()
            continue
        if pending is not None and not plain and urls and any("shazam.com" in url.casefold() for url in urls):
            pending["raw_parts"].append(text or urls[0])
            pending["paragraph_indices"].append(paragraph_index)
            pending["urls"].extend(urls)
            finish_pending()
            continue
        finish_pending()
        if plain:
            current["notes"].append(text)
    finish_pending()

    if set(sections) != set(range(1, 11)):
        raise ReferenceFormatError("reference must contain exactly sections 1) through 10)")
    tests = tuple(ReferenceTest(
        test_id=test_id, source_url=sections[test_id]["source_url"], entries=tuple(sections[test_id]["entries"]),
        notes=tuple(sections[test_id]["notes"]), excluded=test_id == 2,
        exclusion_reason="reference_source_mismatch" if test_id == 2 else None,
    ) for test_id in range(1, 11))
    return OfflineReferenceDocument(document_id, document_path.name, tests)
