"""Structural comparison and diffing for DataDictionary instances."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .models import BusinessLogicRule, DataDictionary


@dataclass
class DiffItem:
    """A single change item."""

    action: str  # "added", "removed", "modified"
    path: str
    old_value: Any = None
    new_value: Any = None

    def to_dict(self) -> dict[str, Any]:
        """Convert diff item to dict."""
        res: dict[str, Any] = {"action": self.action, "path": self.path}
        if self.action == "modified":
            res["old"] = self.old_value
            res["new"] = self.new_value
        elif self.action == "added":
            res["new"] = self.new_value
        elif self.action == "removed":
            res["old"] = self.old_value
        return res


@dataclass
class DiffReport:
    """Structural diff report between two DataDictionary models."""

    items: list[DiffItem] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        """Return True if any changes exist between the two dictionaries."""
        return len(self.items) > 0

    def add(self, action: str, path: str, old_val: Any = None, new_val: Any = None) -> None:
        """Add a diff item."""
        self.items.append(DiffItem(action=action, path=path, old_value=old_val, new_value=new_val))

    def to_dict(self) -> dict[str, Any]:
        """Convert report to a dictionary."""
        return {
            "has_changes": self.has_changes,
            "total_changes": len(self.items),
            "changes": [item.to_dict() for item in self.items],
        }

    def to_json(self, *, indent: int = 2) -> str:
        """Convert report to formatted JSON."""
        return json.dumps(self.to_dict(), indent=indent) + "\n"

    def to_text(self) -> str:
        """Convert report to human-readable text."""
        if not self.has_changes:
            return "No differences found between data dictionaries.\n"

        lines = [f"Data Dictionary Diff ({len(self.items)} changes):"]
        for item in self.items:
            if item.action == "added":
                lines.append(f"  + [Added]    {item.path}: {item.new_value}")
            elif item.action == "removed":
                lines.append(f"  - [Removed]  {item.path}: {item.old_value}")
            elif item.action == "modified":
                lines.append(
                    f"  ~ [Modified] {item.path}: '{item.old_value}' -> '{item.new_value}'"
                )
        return "\n".join(lines) + "\n"

    def to_markdown(self) -> str:
        """Convert report to Markdown."""
        if not self.has_changes:
            return "### Data Dictionary Diff\n\nNo differences found.\n"

        lines = [
            "### Data Dictionary Diff",
            f"\nTotal changes: **{len(self.items)}**\n",
            "| Action | Path | Old Value | New Value |",
            "| :--- | :--- | :--- | :--- |",
        ]
        for item in self.items:
            action_badge = {
                "added": "🟢 Added",
                "removed": "🔴 Removed",
                "modified": "🟡 Modified",
            }.get(item.action, item.action)
            old_str = (
                ""
                if item.old_value is None
                else str(item.old_value).replace("|", "\\|").replace("\r", "").replace("\n", " ")
            )
            new_str = (
                ""
                if item.new_value is None
                else str(item.new_value).replace("|", "\\|").replace("\r", "").replace("\n", " ")
            )
            lines.append(f"| {action_badge} | `{item.path}` | {old_str} | {new_str} |")
        return "\n".join(lines) + "\n"


def _compare_extra_attributes(
    old_extra: dict[str, Any],
    new_extra: dict[str, Any],
    path_prefix: str,
    report: DiffReport,
) -> None:
    all_keys = sorted(set(old_extra.keys()) | set(new_extra.keys()))
    for k in all_keys:
        if k not in old_extra:
            report.add("added", f"{path_prefix}.{k}", new_val=new_extra[k])
        elif k not in new_extra:
            report.add("removed", f"{path_prefix}.{k}", old_val=old_extra[k])
        elif old_extra[k] != new_extra[k]:
            report.add("modified", f"{path_prefix}.{k}", old_extra[k], new_extra[k])


def _rule_key(r: BusinessLogicRule, idx: int) -> str:
    if r.id is not None:
        return str(r.id)
    if r.name is not None:
        return str(r.name)
    if r.sequence is not None:
        return f"seq_{r.sequence}"
    return f"rule_{idx}"


def _compare_rules(
    old_rules: list[BusinessLogicRule],
    new_rules: list[BusinessLogicRule],
    path_prefix: str,
    report: DiffReport,
) -> None:
    old_by_id = {_rule_key(r, idx): r for idx, r in enumerate(old_rules)}
    new_by_id = {_rule_key(r, idx): r for idx, r in enumerate(new_rules)}

    for rid, old_r in old_by_id.items():
        if rid not in new_by_id:
            report.add("removed", f"{path_prefix}.rule[{rid}]", old_val=old_r.name or rid)

    for rid, new_r in new_by_id.items():
        if rid not in old_by_id:
            report.add("added", f"{path_prefix}.rule[{rid}]", new_val=new_r.name or rid)
        else:
            old_r = old_by_id[rid]
            for attr in ("name", "status", "type", "logic", "logicRawXml"):
                ov = getattr(old_r, attr)
                nv = getattr(new_r, attr)
                if ov != nv:
                    report.add("modified", f"{path_prefix}.rule[{rid}].{attr}", ov, nv)
            _compare_extra_attributes(
                old_r.extraAttributes,
                new_r.extraAttributes,
                f"{path_prefix}.rule[{rid}]",
                report,
            )


def diff(old_dict: DataDictionary, new_dict: DataDictionary) -> DiffReport:
    """Compute structural differences between two DataDictionary instances.

    Args:
        old_dict: Baseline DataDictionary.
        new_dict: Updated DataDictionary.

    Returns:
        DiffReport summarizing additions, removals, and modifications.
    """
    report = DiffReport()

    # 1. Compare Metadata (ignoring generatedAt)
    for attr in ("schemaVersion", "studyKey", "source"):
        ov = getattr(old_dict.metadata, attr)
        nv = getattr(new_dict.metadata, attr)
        if ov != nv:
            report.add("modified", f"metadata.{attr}", ov, nv)
    _compare_extra_attributes(
        old_dict.metadata.extraAttributes, new_dict.metadata.extraAttributes, "metadata", report
    )

    # 2. Compare Forms
    old_forms = {f.formKey: f for f in old_dict.forms}
    new_forms = {f.formKey: f for f in new_dict.forms}

    for fk, old_f in old_forms.items():
        if fk not in new_forms:
            report.add("removed", f"forms[{fk}]", old_val=old_f.formName or fk)

    for fk, new_f in new_forms.items():
        if fk not in old_forms:
            report.add("added", f"forms[{fk}]", new_val=new_f.formName or fk)
        else:
            old_f = old_forms[fk]
            for attr in ("formName", "formId", "formType"):
                ov = getattr(old_f, attr)
                nv = getattr(new_f, attr)
                if ov != nv:
                    report.add("modified", f"forms[{fk}].{attr}", ov, nv)
            _compare_extra_attributes(
                old_f.extraAttributes, new_f.extraAttributes, f"forms[{fk}]", report
            )

            # Compare Variables on form
            old_vars = {v.variableName: v for v in old_f.variables}
            new_vars = {v.variableName: v for v in new_f.variables}

            for vn, old_v in old_vars.items():
                if vn not in new_vars:
                    report.add("removed", f"forms[{fk}].variables[{vn}]", old_val=old_v.label or vn)

            for vn, new_v in new_vars.items():
                if vn not in old_vars:
                    report.add("added", f"forms[{fk}].variables[{vn}]", new_val=new_v.label or vn)
                else:
                    old_v = old_vars[vn]
                    for attr in (
                        "label",
                        "variableType",
                        "required",
                        "sequence",
                        "format",
                        "precision",
                    ):
                        ov = getattr(old_v, attr)
                        nv = getattr(new_v, attr)
                        if ov != nv:
                            report.add("modified", f"forms[{fk}].variables[{vn}].{attr}", ov, nv)
                    _compare_extra_attributes(
                        old_v.extraAttributes,
                        new_v.extraAttributes,
                        f"forms[{fk}].variables[{vn}]",
                        report,
                    )

                    # Compare Choices
                    old_choices = {c.choiceValue: c for c in old_v.choices}
                    new_choices = {c.choiceValue: c for c in new_v.choices}

                    for cv, old_c in old_choices.items():
                        if cv not in new_choices:
                            report.add(
                                "removed",
                                f"forms[{fk}].variables[{vn}].choices[{cv}]",
                                old_val=old_c.choiceText,
                            )

                    for cv, new_c in new_choices.items():
                        if cv not in old_choices:
                            report.add(
                                "added",
                                f"forms[{fk}].variables[{vn}].choices[{cv}]",
                                new_val=new_c.choiceText,
                            )
                        else:
                            old_c = old_choices[cv]
                            for attr in ("choiceText", "position"):
                                ov = getattr(old_c, attr)
                                nv = getattr(new_c, attr)
                                if ov != nv:
                                    report.add(
                                        "modified",
                                        f"forms[{fk}].variables[{vn}].choices[{cv}].{attr}",
                                        ov,
                                        nv,
                                    )
                            _compare_extra_attributes(
                                old_c.extraAttributes,
                                new_c.extraAttributes,
                                f"forms[{fk}].variables[{vn}].choices[{cv}]",
                                report,
                            )

            # Compare Business Logic Rules on form
            _compare_rules(old_f.businessLogic, new_f.businessLogic, f"forms[{fk}]", report)

    # 3. Compare Unassigned Business Logic Rules
    _compare_rules(
        old_dict.unassignedBusinessLogic,
        new_dict.unassignedBusinessLogic,
        "unassignedBusinessLogic",
        report,
    )

    return report
