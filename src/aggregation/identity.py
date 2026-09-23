"""Exact ACRID identity nodes and non-resolving metadata diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable


def _id(kind: str, value: Any) -> str:
    digest = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"{kind}_{digest}"


def recording_identity_id(acrid: str | None) -> str | None:
    """Return an exact identity only for a present comparison-normalized ACRID."""
    return _id("recording", {"acrid": acrid}) if acrid else None


@dataclass(frozen=True)
class RecordingIdentity:
    """Evidence node for one exact ACRCloud recording ID; observations remain distinct."""

    identity_id: str
    acrid: str
    observation_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"identity_id": self.identity_id, "acrid": self.acrid, "observation_ids": list(self.observation_ids)}


@dataclass(frozen=True)
class MetadataInconsistency:
    """A non-resolving catalog conflict among observations of one exact ACRID."""

    diagnostic_id: str
    identity_id: str
    field: str
    observed_values: tuple[Any, ...]
    evidence_observation_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "diagnostic_id": self.diagnostic_id,
            "identity_id": self.identity_id,
            "field": self.field,
            "observed_values": [list(value) if isinstance(value, tuple) else value for value in self.observed_values],
            "evidence_observation_ids": list(self.evidence_observation_ids),
        }


def build_identities(observations: Iterable[Any]) -> tuple[tuple[RecordingIdentity, ...], tuple[MetadataInconsistency, ...]]:
    """Group only equal normalized ACRIDs and report, never resolve, conflicts."""
    grouped: dict[str, list[Any]] = {}
    for observation in observations:
        acrid = observation.comparison.acrid
        if acrid:
            grouped.setdefault(acrid, []).append(observation)

    identities, diagnostics = [], []
    fields = ("isrc", "title", "artists", "album", "label", "version", "duration_ms")
    for acrid in sorted(grouped):
        evidence = grouped[acrid]
        identity_id = recording_identity_id(acrid)
        identities.append(RecordingIdentity(identity_id, acrid, tuple(item.observation_id for item in evidence)))
        for field in fields:
            values = [(getattr(item.comparison, field), item.observation_id) for item in evidence]
            known = [(value, observation_id) for value, observation_id in values if value not in (None, (), "")]
            unique = sorted({json.dumps(value, ensure_ascii=False, sort_keys=True) for value, _ in known})
            if len(unique) < 2:
                continue
            observed_values = tuple(json.loads(value) for value in unique)
            evidence_ids = tuple(observation_id for _, observation_id in known)
            diagnostics.append(MetadataInconsistency(
                diagnostic_id=_id("metadata_conflict", {"identity_id": identity_id, "field": field, "values": observed_values, "evidence": evidence_ids}),
                identity_id=identity_id,
                field=field,
                observed_values=observed_values,
                evidence_observation_ids=evidence_ids,
            ))
    return tuple(identities), tuple(diagnostics)
