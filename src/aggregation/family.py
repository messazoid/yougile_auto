"""Explainable recording relations and family-level temporal grouping (S4)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import re
from typing import Any, Iterable

from .normalization import NormalizedCandidateObservation
from .temporal import ExactRecordingOccurrence, TemporalAggregationResult


FAMILY_AGGREGATION_CONTRACT_VERSION = "track-family-aggregation/v1"
_VERSION_WORDS = re.compile(r"\b(?:radio\s+edit|extended(?:\s+(?:mix|version))?|remaster(?:ed)?|remix|live|acoustic|demo|mixed|edit)\b")
_CALIBRATED_VERSION_WORDS = re.compile(r"\b(?:radio\s+edit|extended(?:\s+(?:mix|version))?|remaster(?:ed)?|re[ -]?recorded|remix|mix|live|acoustic|demo|mixed|edit|vocal)\b")
_EMPTY_TITLE = re.compile(r"[\s()\[\]{}._-]+")
_BRACKETED_VERSION = re.compile(r"\([^)]*\b(?:radio\s+edit|extended(?:\s+(?:mix|version))?|remaster(?:ed)?|remix|live|acoustic|demo|mixed|edit)\b[^)]*\)|\[[^]]*\b(?:radio\s+edit|extended(?:\s+(?:mix|version))?|remaster(?:ed)?|remix|live|acoustic|demo|mixed|edit)\b[^]]*\]")
_CALIBRATED_BRACKETED_VERSION = re.compile(r"\(([^)]*\b(?:radio\s+edit|extended(?:\s+(?:mix|version))?|remaster(?:ed)?|re[ -]?recorded|remix|mix|live|acoustic|demo|mixed|edit|vocal)\b[^)]*)\)|\[([^]]*\b(?:radio\s+edit|extended(?:\s+(?:mix|version))?|remaster(?:ed)?|re[ -]?recorded|remix|mix|live|acoustic|demo|mixed|edit|vocal)\b[^]]*)\]")
_FEAT_BRACKET = re.compile(r"[\[(]\s*(?:feat\.?|featuring)\b[^\])]*[\])]", re.IGNORECASE)


@dataclass(frozen=True)
class FamilyTemporalProfile:
    """Versioned, non-production definition of obvious adjacent continuity."""

    name: str
    max_continuity_window_separation: int
    strict_version_signatures: bool = False


OFFLINE_FAMILY_EXPERIMENTAL_V1 = FamilyTemporalProfile("offline-experimental-v1", max_continuity_window_separation=12)

CALIBRATED_FAMILY_V2 = FamilyTemporalProfile(
    "calibrated-v2",
    max_continuity_window_separation=12,
    strict_version_signatures=True,
)


class RecordingRelationKind(StrEnum):
    SAME_ISRC_COMPATIBLE = "same_isrc_compatible"
    SAME_TITLE_ARTIST_COMPATIBLE = "same_title_artist_compatible"
    VERSION_RELATED = "version_related"
    INCOMPATIBLE = "incompatible"
    UNRESOLVED = "unresolved"


def _id(kind: str, material: Any) -> str:
    return f"{kind}_" + hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _core_title(title: str | None, *, calibrated: bool = False) -> str | None:
    if not title:
        return None
    if calibrated:
        core = _FEAT_BRACKET.sub(" ", title)
        core = _CALIBRATED_BRACKETED_VERSION.sub(" ", core)
        core = _CALIBRATED_VERSION_WORDS.sub(" ", core)
    else:
        core = _VERSION_WORDS.sub(" ", _BRACKETED_VERSION.sub(" ", title))
    core = _EMPTY_TITLE.sub(" ", core).strip()
    return core or None


def version_signatures(title: str | None, version: str | None = None) -> frozenset[str]:
    values = []
    if version:
        values.append(_EMPTY_TITLE.sub(" ", version).strip())
    if title:
        for match in _CALIBRATED_BRACKETED_VERSION.finditer(title):
            value = match.group(1) or match.group(2)
            if value:
                values.append(_EMPTY_TITLE.sub(" ", value).strip())
    return frozenset(value for value in values if value)


@dataclass(frozen=True)
class RecordingRelation:
    """A typed, non-destructive relation between two immutable exact identities."""

    relation_id: str
    left_recording_identity_id: str
    right_recording_identity_id: str
    kind: RecordingRelationKind
    evidence_fields: tuple[str, ...]
    evidence_observation_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation_id": self.relation_id, "left_recording_identity_id": self.left_recording_identity_id,
            "right_recording_identity_id": self.right_recording_identity_id, "kind": self.kind.value,
            "evidence_fields": list(self.evidence_fields), "evidence_observation_ids": list(self.evidence_observation_ids),
        }


@dataclass(frozen=True)
class TrackFamily:
    """Audio-recognition grouping, not a work/composition claim."""

    family_id: str
    member_recording_identity_ids: tuple[str, ...]
    relation_ids: tuple[str, ...]
    member_relation_ids: tuple[tuple[str, tuple[str, ...]], ...]
    preferred_core_title: str | None
    artists: tuple[str, ...]
    version_alternatives: tuple[str, ...]
    version_signatures: tuple[str, ...]
    ambiguity_flags: tuple[str, ...]
    preferred_recording_identity_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "family_id": self.family_id, "member_recording_identity_ids": list(self.member_recording_identity_ids),
            "relation_ids": list(self.relation_ids),
            "member_relation_ids": {member: list(relations) for member, relations in self.member_relation_ids},
            "preferred_core_title": self.preferred_core_title, "artists": list(self.artists),
            "version_alternatives": list(self.version_alternatives),
            "version_signatures": list(self.version_signatures),
            "ambiguity_flags": list(self.ambiguity_flags),
            "preferred_recording_identity_id": self.preferred_recording_identity_id,
        }


@dataclass(frozen=True)
class FamilyOccurrence:
    """One temporally continuous appearance of a family, preserving exact members."""

    family_occurrence_id: str
    family_id: str
    exact_occurrence_ids: tuple[str, ...]
    recording_identity_ids: tuple[str, ...]
    source_start: float | int
    source_end: float | int

    def to_dict(self) -> dict[str, Any]:
        return {
            "family_occurrence_id": self.family_occurrence_id, "family_id": self.family_id,
            "exact_occurrence_ids": list(self.exact_occurrence_ids),
            "recording_identity_ids": list(self.recording_identity_ids),
            "source_start": self.source_start, "source_end": self.source_end,
        }


@dataclass(frozen=True)
class FamilyDiagnostic:
    diagnostic_id: str
    kind: str
    relation_id: str

    def to_dict(self) -> dict[str, Any]:
        return {"diagnostic_id": self.diagnostic_id, "kind": self.kind, "relation_id": self.relation_id}


@dataclass(frozen=True)
class FamilyAggregationResult:
    """S4 result retaining all S0-S3 evidence via ``original``."""

    original: TemporalAggregationResult
    relations: tuple[RecordingRelation, ...]
    families: tuple[TrackFamily, ...]
    family_occurrences: tuple[FamilyOccurrence, ...]
    diagnostics: tuple[FamilyDiagnostic, ...]
    profile: FamilyTemporalProfile
    contract_version: str = FAMILY_AGGREGATION_CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version, "profile": self.profile.name,
            "original": self.original.to_dict(), "relations": [item.to_dict() for item in self.relations],
            "families": [item.to_dict() for item in self.families],
            "family_occurrences": [item.to_dict() for item in self.family_occurrences],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


@dataclass(frozen=True)
class _IdentityMetadata:
    observation_ids: tuple[str, ...]
    isrcs: frozenset[str]
    titles: frozenset[str]
    core_titles: frozenset[str]
    artists: frozenset[tuple[str, ...]]
    versions: frozenset[str]
    version_signatures: frozenset[str]


def _identity_metadata(
    observations: Iterable[NormalizedCandidateObservation], profile: FamilyTemporalProfile,
) -> dict[str, _IdentityMetadata]:
    grouped: dict[str, list[NormalizedCandidateObservation]] = {}
    for item in observations:
        if item.recording_identity_id:
            grouped.setdefault(item.recording_identity_id, []).append(item)
    result = {}
    for identity, items in grouped.items():
        result[identity] = _IdentityMetadata(
            observation_ids=tuple(item.observation_id for item in items),
            isrcs=frozenset(item.comparison.isrc for item in items if item.comparison.isrc),
            titles=frozenset(item.comparison.title for item in items if item.comparison.title),
            core_titles=frozenset(
                core for item in items
                if (core := _core_title(item.comparison.title, calibrated=profile.strict_version_signatures))
            ),
            artists=frozenset(item.comparison.artists for item in items if item.comparison.artists),
            versions=frozenset(version for item in items for version in item.comparison.version_descriptors),
            version_signatures=frozenset(
                signature for item in items
                for signature in version_signatures(item.comparison.title, item.comparison.version)
            ),
        )
    return result


def _pair_kind(
    left: _IdentityMetadata, right: _IdentityMetadata, profile: FamilyTemporalProfile,
) -> tuple[RecordingRelationKind, tuple[str, ...]] | None:
    shared_isrc = left.isrcs & right.isrcs
    shared_core_title = left.core_titles & right.core_titles
    compatible_artists = any(set(left_artists) & set(right_artists) for left_artists in left.artists for right_artists in right.artists)
    both_known_artists = bool(left.artists and right.artists)
    remix_conflict = ("remix" in left.versions) != ("remix" in right.versions)
    shared_titles = left.titles & right.titles
    signature_conflict = bool(
        profile.strict_version_signatures
        and left.version_signatures and right.version_signatures
        and not (left.version_signatures & right.version_signatures)
    )
    if shared_isrc:
        if shared_core_title and compatible_artists:
            return RecordingRelationKind.SAME_ISRC_COMPATIBLE, ("isrc", "core_title", "artists")
        return RecordingRelationKind.INCOMPATIBLE, ("isrc", "metadata_incompatible")
    if shared_core_title and compatible_artists:
        if signature_conflict:
            return RecordingRelationKind.INCOMPATIBLE, ("core_title", "artists", "version_signature_conflict")
        if profile.strict_version_signatures and bool(left.version_signatures) != bool(right.version_signatures) and not shared_titles:
            return RecordingRelationKind.UNRESOLVED, ("core_title", "artists", "version_metadata_incomplete")
        if remix_conflict:
            return RecordingRelationKind.INCOMPATIBLE, ("core_title", "artists", "remix_conflict")
        if left.versions != right.versions and (left.versions or right.versions):
            return RecordingRelationKind.VERSION_RELATED, ("core_title", "artists", "version_descriptors")
        return RecordingRelationKind.SAME_TITLE_ARTIST_COMPATIBLE, ("core_title", "artists")
    if shared_core_title and both_known_artists:
        return RecordingRelationKind.INCOMPATIBLE, ("core_title", "artists_incompatible")
    if shared_core_title or shared_isrc:
        return RecordingRelationKind.UNRESOLVED, ("insufficient_metadata",)
    return None


def _relations(
    metadata: dict[str, _IdentityMetadata], profile: FamilyTemporalProfile,
) -> tuple[RecordingRelation, ...]:
    candidates: set[tuple[str, str]] = set()
    by_isrc: dict[str, list[str]] = {}
    by_core_title: dict[str, list[str]] = {}
    for identity, item in metadata.items():
        for value in item.isrcs:
            by_isrc.setdefault(value, []).append(identity)
        for value in item.core_titles:
            by_core_title.setdefault(value, []).append(identity)
    for groups in (by_isrc.values(), by_core_title.values()):
        for identities in groups:
            ordered = sorted(identities)
            candidates.update((left, right) for index, left in enumerate(ordered) for right in ordered[index + 1:])
    relations = []
    for left_id, right_id in sorted(candidates):
        kind_and_fields = _pair_kind(metadata[left_id], metadata[right_id], profile)
        if kind_and_fields is None:
            continue
        kind, fields = kind_and_fields
        evidence = tuple(sorted(metadata[left_id].observation_ids + metadata[right_id].observation_ids))
        relations.append(RecordingRelation(
            relation_id=_id("recording_relation", {"left": left_id, "right": right_id, "kind": kind.value, "fields": fields, "evidence": evidence}),
            left_recording_identity_id=left_id, right_recording_identity_id=right_id,
            kind=kind, evidence_fields=fields, evidence_observation_ids=evidence,
        ))
    return tuple(relations)


def _families(metadata: dict[str, _IdentityMetadata], relations: tuple[RecordingRelation, ...]) -> tuple[TrackFamily, ...]:
    positive = {RecordingRelationKind.SAME_ISRC_COMPATIBLE, RecordingRelationKind.SAME_TITLE_ARTIST_COMPATIBLE, RecordingRelationKind.VERSION_RELATED}
    parent = {identity: identity for identity in metadata}
    def find(identity: str) -> str:
        while parent[identity] != identity:
            parent[identity] = parent[parent[identity]]
            identity = parent[identity]
        return identity
    for relation in relations:
        if relation.kind in positive:
            left, right = find(relation.left_recording_identity_id), find(relation.right_recording_identity_id)
            if left != right:
                parent[right] = left
    groups: dict[str, list[str]] = {}
    for identity in sorted(metadata):
        groups.setdefault(find(identity), []).append(identity)
    families = []
    for members in sorted((tuple(sorted(value)) for value in groups.values())):
        member_set = set(members)
        internal = tuple(item for item in relations if {item.left_recording_identity_id, item.right_recording_identity_id} <= member_set)
        relation_ids = tuple(item.relation_id for item in internal if item.kind in positive)
        member_relations = tuple((member, tuple(item.relation_id for item in internal if member in (item.left_recording_identity_id, item.right_recording_identity_id) and item.kind in positive)) for member in members)
        common_titles = set(metadata[members[0]].core_titles)
        common_artists = set(metadata[members[0]].artists)
        for member in members[1:]:
            common_titles &= metadata[member].core_titles
            common_artists &= metadata[member].artists
        versions = tuple(sorted({version for member in members for version in metadata[member].versions}))
        signatures = tuple(sorted({signature for member in members for signature in metadata[member].version_signatures}))
        flags = []
        if len(members) > 1:
            flags.append("ambiguous_recording")
        if any(item.kind is RecordingRelationKind.INCOMPATIBLE for item in internal):
            flags.append("internal_metadata_incompatibility")
        families.append(TrackFamily(
            family_id=_id("track_family", {"members": members}), member_recording_identity_ids=members,
            relation_ids=relation_ids, member_relation_ids=member_relations,
            preferred_core_title=sorted(common_titles)[0] if len(common_titles) == 1 else None,
            artists=sorted(common_artists)[0] if len(common_artists) == 1 else (),
            version_alternatives=versions, version_signatures=signatures,
            ambiguity_flags=tuple(flags),
            preferred_recording_identity_id=None,
        ))
    return tuple(families)


def _family_occurrences(temporal: TemporalAggregationResult, families: tuple[TrackFamily, ...], profile: FamilyTemporalProfile) -> tuple[FamilyOccurrence, ...]:
    family_by_identity = {identity: family.family_id for family in families for identity in family.member_recording_identity_ids}
    grouped: dict[str, list[ExactRecordingOccurrence]] = {}
    for occurrence in temporal.occurrences:
        grouped.setdefault(family_by_identity[occurrence.recording_identity_id], []).append(occurrence)
    result = []
    for family_id, occurrences in sorted(grouped.items()):
        current: list[ExactRecordingOccurrence] = []
        for occurrence in sorted(occurrences, key=lambda item: (item.observed_envelope_start, item.occurrence_id)):
            if current and occurrence.supports[0].window_index - current[-1].supports[-1].window_index > profile.max_continuity_window_separation:
                result.append(_make_family_occurrence(family_id, current))
                current = []
            current.append(occurrence)
        if current:
            result.append(_make_family_occurrence(family_id, current))
    return tuple(sorted(result, key=lambda item: (item.source_start, item.family_id, item.family_occurrence_id)))


def _make_family_occurrence(family_id: str, occurrences: list[ExactRecordingOccurrence]) -> FamilyOccurrence:
    exact_ids = tuple(item.occurrence_id for item in occurrences)
    identities = tuple(dict.fromkeys(item.recording_identity_id for item in occurrences))
    return FamilyOccurrence(
        family_occurrence_id=_id("family_occurrence", {"family": family_id, "exact_occurrences": exact_ids}),
        family_id=family_id, exact_occurrence_ids=exact_ids, recording_identity_ids=identities,
        source_start=occurrences[0].observed_envelope_start, source_end=occurrences[-1].observed_envelope_end,
    )


def aggregate_families(temporal: TemporalAggregationResult, profile: FamilyTemporalProfile = OFFLINE_FAMILY_EXPERIMENTAL_V1) -> FamilyAggregationResult:
    """Build only explainable family hypotheses; no score, conflict, or transition decisions."""
    observations = [item for window in temporal.original.windows for item in window.candidates]
    metadata = _identity_metadata(observations, profile)
    relations = _relations(metadata, profile)
    families = _families(metadata, relations)
    diagnostics = tuple(FamilyDiagnostic(
        diagnostic_id=_id("family_diagnostic", {"relation": relation.relation_id, "kind": relation.kind.value}),
        kind=relation.kind.value, relation_id=relation.relation_id,
    ) for relation in relations if relation.kind in {RecordingRelationKind.INCOMPATIBLE, RecordingRelationKind.UNRESOLVED})
    return FamilyAggregationResult(temporal, relations, families, _family_occurrences(temporal, families, profile), diagnostics, profile)
