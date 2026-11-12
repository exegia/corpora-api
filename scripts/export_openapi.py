"""Write the exact FastAPI OpenAPI schema consumed by the Fumapress site."""

import json
import sys
from pathlib import Path

from corpora_api.app import app


def main() -> None:
    target = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
    content = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
    if "--check" in sys.argv[1:]:
        if not target.exists() or target.read_text() != content:
            raise SystemExit(
                "docs/openapi.json is stale; run uv run python scripts/export_openapi.py"
            )
        print("docs/openapi.json is current")
        return
    target.write_text(content)
    print(f"Wrote {target}")


if __name__ == "__main__":
    main()
