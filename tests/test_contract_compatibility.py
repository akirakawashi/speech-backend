"""a contract cannot change without someone deciding that it changed.

`speech-ms` and its front end ship as separate images, which means they can be
deployed independently -- and independent deployment is precisely when a caller
running one version meets a provider running another. A field renamed in a
pydantic model would otherwise be a green test run and a broken screen in
production the moment the two sides drifted apart.

The tests here are two different questions:

1. **Did the shape change without the snapshot being updated?** A drift check.
   It fails on *any* difference, additive or not.

2. **If it changed, which kind of change was it?** The failure message
   classifies the diff so the required version bump is not a judgement call:
   removed fields, added required fields and changed types are breaking; a new
   optional field is additive.

Carried over from the monorepo, where the same module also guarded the
contracts of the chat platform. The three tests that exercised those (unknown
fields on a workflow model, version negotiation of the workflow engine, the DWH
export N-1 check) stayed there with the code they test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.contract_registry import CONTRACTS, Contract, schema_of

SNAPSHOT_DIR = Path(__file__).parent / "contracts"


def _wire_shape(value: Any, *, schema_node: bool = False) -> Any:
    """Убрать описания документации, сохранив все типы и ограничения JSON Schema."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if schema_node and key == "description":
                continue
            if schema_node and key in {"default", "const", "enum", "examples"}:
                result[key] = item
            else:
                nested_schema = not schema_node or key not in {
                    "properties",
                    "$defs",
                    "definitions",
                    "patternProperties",
                    "dependentSchemas",
                }
                result[key] = _wire_shape(item, schema_node=nested_schema)
        return result
    if isinstance(value, list):
        return [_wire_shape(item, schema_node=schema_node) for item in value]
    return value


def _snapshot(contract: Contract) -> dict[str, Any]:
    path = SNAPSHOT_DIR / f"{contract.name}-{contract.version}.json"
    if not path.exists():
        pytest.fail(
            f"No snapshot for {contract.name} at version {contract.version}. "
            f"The version constant was bumped without recording the new shape: run "
            f"`python scripts/update_contract_snapshots.py`."
        )
    snapshot: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return snapshot


def _fields(schema: dict[str, Any], model: str) -> dict[str, dict[str, Any]]:
    properties: dict[str, dict[str, Any]] = schema.get(model, {}).get("properties", {})
    return properties


def _required(schema: dict[str, Any], model: str) -> set[str]:
    return set(schema.get(model, {}).get("required", []))


def _classify(old: dict, new: dict) -> tuple[list[str], list[str]]:
    """Split the diff into breaking and additive changes."""
    breaking: list[str] = []
    additive: list[str] = []

    for model in sorted(set(old) | set(new)):
        if model not in new:
            breaking.append(f"{model}: model removed")
            continue
        if model not in old:
            additive.append(f"{model}: model added")
            continue

        old_fields, new_fields = _fields(old, model), _fields(new, model)
        old_required, new_required = _required(old, model), _required(new, model)

        for field in sorted(set(old_fields) - set(new_fields)):
            breaking.append(f"{model}.{field}: field removed")
        for field in sorted(set(new_fields) - set(old_fields)):
            if field in new_required:
                breaking.append(f"{model}.{field}: required field added")
            else:
                additive.append(f"{model}.{field}: optional field added")
        for field in sorted(set(old_fields) & set(new_fields)):
            if old_fields[field] != new_fields[field]:
                breaking.append(f"{model}.{field}: type or constraints changed")
            if field in new_required - old_required:
                breaking.append(f"{model}.{field}: became required")
    return breaking, additive


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda c: c.name)
def test_wire_shape_matches_the_recorded_snapshot(contract: Contract) -> None:
    """The committed snapshot is the contract; the models must still match it."""
    recorded = _snapshot(contract)
    current = _wire_shape(schema_of(contract))
    recorded_shape = _wire_shape(recorded["schemas"])

    if recorded_shape == current:
        return

    breaking, additive = _classify(recorded_shape, current)
    required_bump = "MAJOR" if breaking else "MINOR"
    detail = "\n".join(
        [f"  BREAKING  {c}" for c in breaking] + [f"  additive  {c}" for c in additive]
    )
    pytest.fail(
        f"{contract.name} changed shape while still claiming version "
        f"{contract.version}.\n{detail}\n\n"
        f"Required: bump the version constant ({required_bump}), then run "
        f"`python scripts/update_contract_snapshots.py`. Keep the old snapshot "
        f"file -- it is what the N/N-1 check reads."
    )


def test_docstrings_can_change_without_hiding_a_wire_constraint_change() -> None:
    """Докстринг не меняет контракт; диапазон числа и обязательность поля меняют."""
    old = {"Result": {"description": "old", "properties": {"score": {"type": "integer"}}}}
    described = {"Result": {"description": "new", "properties": {"score": {"type": "integer"}}}}
    constrained = {"Result": {"properties": {"score": {"type": "integer", "minimum": 0}}}}
    required = {"Result": {"properties": {"score": {"type": "integer"}}, "required": ["score"]}}
    assert _wire_shape(old) == _wire_shape(described)
    assert _classify(_wire_shape(old), _wire_shape(constrained))[0]
    assert _classify(_wire_shape(old), _wire_shape(required))[0]
    described_field = {"Result": {"properties": {"description": {"type": "string"}}}}
    assert _wire_shape(described_field) == described_field


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda c: c.name)
def test_snapshot_records_the_version_it_is_named_for(contract: Contract) -> None:
    """A snapshot whose contents disagree with its filename cannot be trusted."""
    recorded = _snapshot(contract)
    assert recorded["version"] == contract.version
    assert recorded["contract"] == contract.name


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda c: c.name)
def test_every_earlier_snapshot_is_still_readable(contract: Contract) -> None:
    """Older snapshots stay parseable and keep their identity.

    They are the only record of what a not-yet-redeployed caller sends. A
    snapshot that is deleted or rewritten in place takes the N/N-1 answer with
    it, so this notices the loss at the next test run rather than during the
    deploy that needed it.
    """
    for path in sorted(SNAPSHOT_DIR.glob(f"{contract.name}-*.json")):
        recorded = json.loads(path.read_text(encoding="utf-8"))
        assert recorded["contract"] == contract.name
        assert recorded["schemas"], f"{path.name} records no schemas"
        assert path.name == f"{contract.name}-{recorded['version']}.json"
