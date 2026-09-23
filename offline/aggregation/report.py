"""Deterministic human and machine report helpers for offline S9 validation."""

from __future__ import annotations

from pathlib import Path

from .validation import MatchState, ValidationResult, serialize_validation_result


def render_human_report(result: ValidationResult) -> str:
    """Render compact review text; IDs and ranges point back into S8 evidence."""
    lines = [
        "# Offline reference validation v1.1", "",
        f"Comparable tests: {result.summary.comparable_test_count}; excluded: {', '.join(map(str, result.summary.excluded_test_ids)) or 'none'}.",
        f"Reference entries: {result.summary.reference_entry_count}; occurrence-detected: {result.summary.occurrence_detected_count}; family-candidate-only: {result.summary.family_candidate_only_count}; secondary-only: {result.summary.secondary_only_count}; ambiguous: {result.summary.ambiguous_match_count}; missed(no-evidence/usable-evidence): {result.summary.missed_no_usable_evidence_count}/{result.summary.missed_with_usable_evidence_count}.",
        f"Additional appearances: {result.summary.additional_appearance_count}; ambiguous-linked appearances: {result.summary.ambiguous_linked_appearance_count}; possible fragmentation: {result.summary.fragmentation_candidate_count}; repeat-supported: {result.summary.repeat_supported_count}; overmerge: {result.summary.overmerge_candidate_count}.",
        f"Conflicts: {result.summary.conflict_count}; singletons: {result.summary.singleton_count}; re-entry: {result.summary.reentry_count}; version churn: {result.summary.version_churn_appearance_count}; bridged-gap appearances: {result.summary.bridged_gap_appearance_count}.",
    ]
    for test in result.tests:
        detected = sum(item.state is MatchState.OCCURRENCE_DETECTED for item in test.matches)
        missed = sum(item.state in {MatchState.MISSED_NO_USABLE_EVIDENCE, MatchState.MISSED_WITH_USABLE_EVIDENCE} for item in test.matches)
        ambiguous = sum(item.state is MatchState.AMBIGUOUS_MATCH for item in test.matches)
        lines.extend(["", f"## Test {test.test_id}" + (f" — EXCLUDED: {test.exclusion_reason}" if test.excluded else ""),
                      f"Run: `{test.run_id}`; reference entries: {test.reference_entry_count}; detected/missed/ambiguous: {detected}/{missed}/{ambiguous}; ambiguous-linked: {len(test.ambiguous_linked_appearance_ids)}."])
        for entry in test.reference_entries:
            match = next(item for item in test.matches if item.reference_entry_id == entry.entry_id)
            target = match.matched_family_id or ", ".join(match.candidate_family_ids) or "none"
            lines.append(f"- Ref {entry.order}: {entry.title} — {entry.artists}; {match.state.value}; family `{target}`; {match.reason}.")
        if test.additional_appearances:
            lines.append("- Additional appearances: " + "; ".join(
                f"`{item.appearance_id}`/{item.family_id} {item.title or 'unknown'} — {', '.join(item.artists) or 'unknown'} [{item.observed_start}, {item.observed_end}], support={item.top_support_windows}, gaps={item.total_internal_gaps}, ACRIDs={item.distinct_acrid_count}, singleton={item.singleton}, conflicts={len(item.conflict_ids)}; {item.reason}" for item in test.additional_appearances[:10]
            ) + ("; …" if len(test.additional_appearances) > 10 else ""))
        for item in test.fragmentation_candidates:
            lines.append(f"- {item.classification}: family `{item.family_id}`, appearances {len(item.appearance_ids)}, refs {len(item.reference_entry_ids)}; {item.reason}.")
        for item in test.overmerge_candidates:
            lines.append(f"- OVERMERGE CANDIDATE: family `{item.family_id}`; {item.reason}.")
        if test.duplicate_reference_cases:
            lines.append(f"- Duplicate reference cases preserved: {len(test.duplicate_reference_cases)}.")
        lines.append(f"- Conflicts: {len(test.conflict_ids)}; singletons: {len(test.singleton_appearance_ids)}; re-entry: {len(test.reentry_appearance_ids)}; transitions: {len(test.transition_ids)}; version churn: {len(test.version_churn_appearance_ids)}; bridged gaps: {len(test.bridged_gap_appearance_ids)}; gap-fragmentation overlap: {len(test.gap_fragmentation_appearance_ids)}.")
    lines.extend(["", "## Regression matrix"])
    lines.extend(f"- {item.regression_id}: {item.state.value}; {item.reason}; evidence: {', '.join(item.evidence_ids) or 'none'}." for item in result.regression_matrix)
    lines.extend(["", "## Reconciliation and DB-readiness", f"- Reconciled: {result.summary.reconciliation_valid}.", f"- DB-readiness: {result.db_readiness_gate.state.value}; reasons: {', '.join(result.db_readiness_gate.reason_codes) or 'none'}." ])
    return "\n".join(lines) + "\n"


def write_validation_reports(result: ValidationResult, output_directory: str | Path) -> tuple[Path, Path]:
    """Write deterministic offline artifacts; callers choose the destination."""
    directory = Path(output_directory)
    directory.mkdir(parents=True, exist_ok=True)
    machine = directory / "reference_validation_v1_1.json"
    human = directory / "reference_validation_v1_1.md"
    machine.write_text(serialize_validation_result(result) + "\n", encoding="utf-8")
    human.write_text(render_human_report(result), encoding="utf-8")
    return machine, human
