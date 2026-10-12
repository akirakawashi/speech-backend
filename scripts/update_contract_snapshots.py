#!/usr/bin/env python3
"""Write the committed schema snapshot for every contract at its current version.

Run this deliberately, after deciding that a contract change is intended and
that its version constant is right. The snapshot is what
`tests/test_contract_compatibility.py` compares against, so regenerating it is
the act of accepting a change -- which is why it lives behind a command instead
of happening automatically when a test fails.

    python scripts/update_contract_snapshots.py

Old snapshots are never deleted. They are how the N/N-1 compatibility matrix
knows what the previous version looked like; removing one erases the ability to
tell whether the next change is safe for a caller that has not been redeployed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.contract_registry import CONTRACTS, schema_of  # noqa: E402

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "tests" / "contracts"


def main() -> int:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    for contract in CONTRACTS:
        path = SNAPSHOT_DIR / f"{contract.name}-{contract.version}.json"
        payload = {
            "contract": contract.name,
            "version": contract.version,
            "direction": contract.direction,
            "schemas": schema_of(contract),
        }
        existed = path.exists()
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"{'updated' if existed else 'created'}: {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
