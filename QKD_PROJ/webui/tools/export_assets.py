"""Copy the Pixel Agents assets into this repository, once, with provenance.

    py -3 QKD_PROJ/webui/tools/export_assets.py            # export
    py -3 QKD_PROJ/webui/tools/export_assets.py --check    # fail if it drifted

Why a copy rather than a path into ``agent-platform/``:

**The web UI must not depend on a sibling checkout.** ``agent-platform/`` is a
fork that can be updated, moved or removed. A server that read its assets from
there would render differently depending on what somebody did to a directory
next door, and would stop rendering at all if they deleted it.

**Nothing is fetched at runtime.** Not from a CDN, not from ``node_modules``,
not from the fork. The backend serves files from ``webui/assets/`` and there is
a test that says so. An office that phones home for its sprites is an office
that looks different on a machine with no network -- which is the machine this
project is usually on.

**The copy is only as good as its record.** So every file carries a SHA-256, the
manifest carries the upstream commit it came from, and ``--check`` re-hashes
everything. A vendored tree with no digests is a tree nobody can tell has been
edited.

The manifest also records what the licence obliges: MIT, its copyright line,
and the character pack's separate attribution. Those travel with the bytes,
because a licence in a directory nobody copies is a licence nobody honours.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
UPSTREAM = ROOT / "agent-platform"
DESTINATION = Path(__file__).resolve().parents[1] / "assets"

# The revision this copy is taken from. `docs/platform-integration.md` pins the
# fork to it, and the manifest records it so a future update is a diff rather
# than a guess.
PINNED = "3537e140c2094761beae748592aeb92ece8edfdd"

# What is copied, and from where. Anything not listed is not vendored -- the
# banner and the screenshot are README furniture, not office assets.
SOURCES: tuple[tuple[str, str], ...] = (
    ("webview-ui/public/assets", "assets"),
    ("webview-ui/public/characters.png", "characters.png"),
    ("webview-ui/public/fonts", "fonts"),
    ("LICENSE", "LICENSE-pixel-agents"),
)

PROVENANCE = {
    "upstream": {
        "name": "Pixel Agents",
        "repository": "https://github.com/pixel-agents-hq/pixel-agents",
        "version": "1.4.1",
        "commit": PINNED,
        "commit_date": "2026-08-15",
        "license": "MIT",
        "copyright": "Copyright (c) 2026 Pablo De Lucca",
        "license_file": "LICENSE-pixel-agents",
    },
    "attribution": [
        {
            "applies_to": "assets/characters/",
            "note": (
                "The six characters are based on the work of JIK-A-4, Metro City "
                "(free top-down character pack), as stated in the upstream README."
            ),
            "source": "https://jik-a-4.itch.io/metrocity-free-topdown-character-pack",
        },
        {
            "applies_to": "fonts/",
            "note": (
                "FS Pixel Sans Unicode, shipped by upstream. No separate licence file "
                "accompanies it there; this copy carries the same terms as the tree it "
                "came from and no claim beyond that is made here."
            ),
        },
    ],
    "rules": [
        "Nothing here is fetched at runtime: the backend serves these files from disk.",
        "Every file is hashed. `export_assets.py --check` re-hashes and fails on drift.",
        "This is a copy, not a link. agent-platform/ may be updated or removed without "
        "changing what the web UI renders.",
    ],
}

SCRIPT_VERSION = 1
MANIFEST = DESTINATION / "MANIFEST.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def walk(destination: Path) -> list[Path]:
    """Every exported file, sorted, with the manifest itself left out."""
    return sorted(
        p for p in destination.rglob("*") if p.is_file() and p.resolve() != MANIFEST.resolve()
    )


def build_manifest(destination: Path, commit: str) -> dict:
    files = []
    for path in walk(destination):
        files.append(
            {
                "path": path.relative_to(destination).as_posix(),
                "sha256": digest(path),
                "bytes": path.stat().st_size,
            }
        )
    # One digest over the whole set, so "has anything changed" is one
    # comparison rather than a loop a caller might get wrong.
    rolled = hashlib.sha256()
    for entry in files:
        rolled.update(entry["path"].encode("utf-8"))
        rolled.update(bytes.fromhex(entry["sha256"]))

    return {
        "manifest_version": SCRIPT_VERSION,
        "exported_from": {
            **PROVENANCE["upstream"],
            "checkout_commit": commit,
            "assets_unchanged_since_pin": unchanged_since_pin(),
        },
        "attribution": PROVENANCE["attribution"],
        "rules": PROVENANCE["rules"],
        "file_count": len(files),
        "tree_sha256": rolled.hexdigest(),
        "files": files,
    }


def unchanged_since_pin() -> str:
    """Whether the asset paths differ between the pin and the checkout's HEAD.

    The fork carries its own commits on top of upstream 1.4.1, so "these came
    from 1.4.1" is a claim, not a given. Rather than assert it, the export asks
    git and records the answer -- including "could not tell", which is a
    different thing from "yes".
    """
    if not (UPSTREAM / ".git").exists():
        return "unknown: agent-platform is not a git checkout here"
    paths = [source for source, _ in SOURCES]
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", PINNED, "HEAD", "--", *paths],
            cwd=UPSTREAM,
            capture_output=True,
            text=True,
            check=True,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", *paths],
            cwd=UPSTREAM,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"unknown: {exc}"

    changed = [line for line in diff.stdout.splitlines() if line.strip()]
    uncommitted = [line for line in dirty.stdout.splitlines() if line.strip()]
    if changed or uncommitted:
        return (
            f"no: {len(changed)} committed and {len(uncommitted)} uncommitted change(s) to the "
            "asset paths since the pin"
        )
    return "yes: byte-identical to the pinned upstream, and the checkout is clean"


def checkout_commit() -> str:
    """What the sibling checkout is actually at, which may not be the pin."""
    if not (UPSTREAM / ".git").exists():
        return "unknown: agent-platform is not a git checkout here"
    try:
        found = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=UPSTREAM, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return found.stdout.strip()


def export(destination: Path) -> dict:
    if not UPSTREAM.is_dir():
        raise SystemExit(
            f"{UPSTREAM} is not here. The export needs the upstream checkout once; after that "
            "the copy under webui/assets/ is what the server uses."
        )
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    for source_rel, target_rel in SOURCES:
        source = UPSTREAM / source_rel
        target = destination / target_rel
        if not source.exists():
            raise SystemExit(f"missing from upstream: {source}")
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    manifest = build_manifest(destination, checkout_commit())
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def check(destination: Path) -> list[str]:
    """Every way the copy can disagree with its record."""
    problems: list[str] = []
    if not MANIFEST.is_file():
        return [f"{MANIFEST} does not exist; run the export"]

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    recorded = {entry["path"]: entry for entry in manifest.get("files", [])}
    on_disk = {p.relative_to(destination).as_posix(): p for p in walk(destination)}

    for missing in sorted(set(recorded) - set(on_disk)):
        problems.append(f"recorded but not on disk: {missing}")
    for extra in sorted(set(on_disk) - set(recorded)):
        problems.append(f"on disk but not recorded: {extra}")
    for name in sorted(set(recorded) & set(on_disk)):
        actual = digest(on_disk[name])
        if actual != recorded[name]["sha256"]:
            problems.append(
                f"{name}: manifest {recorded[name]['sha256'][:12]}, file {actual[:12]}"
            )

    if not problems:
        rebuilt = build_manifest(destination, manifest["exported_from"].get("checkout_commit", ""))
        if rebuilt["tree_sha256"] != manifest.get("tree_sha256"):
            problems.append("the rolled tree digest disagrees with the per-file digests")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=DESTINATION)
    parser.add_argument("--check", action="store_true", help="exit 1 if the copy drifted")
    args = parser.parse_args(argv)

    if args.check:
        problems = check(args.destination)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0

    manifest = export(args.destination)
    print(
        f"exported {manifest['file_count']} files from {manifest['exported_from']['name']} "
        f"{manifest['exported_from']['version']} ({manifest['exported_from']['commit'][:12]})"
    )
    print(f"tree sha256 {manifest['tree_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
