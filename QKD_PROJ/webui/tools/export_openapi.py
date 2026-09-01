"""Freeze the browser contract as OpenAPI, so TypeScript can be generated from it.

    py -3 QKD_PROJ/webui/tools/export_openapi.py            # write the schema
    py -3 QKD_PROJ/webui/tools/export_openapi.py --check    # fail if it drifted

    npx openapi-typescript QKD_PROJ/webui/frontend/src/api/openapi.json \\
        -o QKD_PROJ/webui/frontend/src/api/schema.d.ts

The chain is Pydantic -> OpenAPI -> TypeScript, and the reason it is a chain
rather than two hand-written type declarations is that hand-written ones agree
until they do not. A field renamed in ``models.py`` changes this file, which
changes ``schema.d.ts``, which stops the frontend compiling. A field renamed
with types written twice produces ``undefined`` at runtime in a browser nobody
is watching.

``--check`` is what the test asserts. The file is committed so a reviewer can
see the contract change in the diff, and regenerating it is one command.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from QKD_PROJ.webui.backend.app import create_app  # noqa: E402
from QKD_PROJ.webui.backend.security import Session  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "frontend" / "src" / "api" / "openapi.json"


def schema() -> dict:
    """The app's OpenAPI document, with nothing run-specific in it.

    A session with no file and no daemon address: generating a schema must not
    write a token anywhere or try to reach a pipe.
    """
    app = create_app(address=None, session=Session(file=None))
    return app.openapi()


def render() -> str:
    return json.dumps(schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true", help="exit 1 if the file is stale")
    args = parser.parse_args(argv)

    current = render()
    if args.check:
        if not args.out.exists():
            print(f"{args.out} does not exist; run this script without --check", file=sys.stderr)
            return 1
        if args.out.read_text(encoding="utf-8") != current:
            print(
                f"{args.out} is stale. The browser contract changed; regenerate it and "
                "regenerate schema.d.ts with openapi-typescript.",
                file=sys.stderr,
            )
            return 1
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(current, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
