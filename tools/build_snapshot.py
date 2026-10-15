"""Build the public snapshot of CareerOS (release blocker B2, 2026-10-14): an allowlisted copy of one commit with a few
fixed replacements, ready for a fresh Git history. The procedure around it is in docs/public_release_checklist.md.

    .venv-dev\\Scripts\\python.exe tools\\build_snapshot.py OUT_DIR --owner NAME [--commit REF]

1. Exports the allowlisted paths of a commit (never the working tree) into a new, empty OUT_DIR.
2. Applies the fixed replacements:
   - config/employers.yaml: a fictional seed list (the real one names the employers the job seeker follows);
   - LICENSE: MIT, in the GitHub user's name;
   - the Power BI project's ExportFolder: a neutral folder. The .pbix is never copied: it stores a local folder path.
3. Writes the manifest (source commit, date, each file's SHA-256) to release/ in THIS repository, never the snapshot.

It creates no Git history and publishes nothing; both are separate, human-approved steps. The evidence files are
regenerated inside the snapshot afterwards (tools/mvp_gate.py, then tools/mutate.py).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ("careeros", "tests", "tools", "config/workflows", "config/policy/defaults.yaml", "config/site_policy.yaml",
             "config/employers.yaml", "docs", "samples/analytics_demo", "powerbi/CareerOS.pbip", "powerbi/CareerOS.SemanticModel",
             "powerbi/CareerOS.Report", "powerbi/screenshots", ".github", "README.md", "SECURITY.md", "PRIVACY.md",
             ".gitignore", ".gitattributes", ".env.example", "pyproject.toml", "requirements.lock", "requirements-dev.lock")
NEVER = ("docs/operator_manual.md",)          # inside an allowlisted folder, but private
NEUTRAL_FOLDER = r"C:\CareerOS\samples\analytics_demo"
MODEL = "powerbi/CareerOS.SemanticModel/model.bim"
EMPLOYERS = """\
# Public job boards to poll. These entries are FICTIONAL examples: replace them with the employers you follow
# (`careeros employers add`, or paste a Greenhouse, Lever or Ashby job link and the board is added for you).
employers:
  - {name: Example Health Plan, parent: Example Health Plan, ats: greenhouse, token: examplehealthplan, industry: health insurance}
  - {name: Sample Claims Services, parent: Sample Claims Services, ats: lever, token: sampleclaims, industry: claims services}
  - {name: Demo Benefits Co, parent: Demo Benefits Co, ats: ashby, token: demobenefits, industry: health benefits}
"""
MIT = """\
MIT License

Copyright (c) 2026 {owner}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=True).stdout


def export(commit: str, out: Path) -> list[str]:
    """Extract the allowlisted paths of COMMIT into OUT, leaving out NEVER. Returns the files written."""
    archive = git("archive", "--format=tar", commit, "--", *ALLOWLIST)
    written = []
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if not member.isfile() or member.name in NEVER:
                continue
            target = out / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())
            written.append(member.name)
    return written


def neutral_folder(model_text: str) -> str:
    """The Power BI model with its ExportFolder parameter set to the neutral folder (the only place a local path is)."""
    model = json.loads(model_text)
    old = next(e for e in model["model"]["expressions"] if e["name"] == "ExportFolder")["expression"]
    new = re.sub(r'^".*?" meta', lambda _: '"' + NEUTRAL_FOLDER.replace('"', '""') + '" meta', old)
    # replace the string exactly as it is written, so the file stays byte-identical to the generator's output
    old_json, new_json = json.dumps(old)[1:-1], json.dumps(new)[1:-1]
    assert model_text.count(old_json) == 1, "ExportFolder expression not found exactly once"
    return model_text.replace(old_json, new_json)


def replace(out: Path, owner: str) -> None:
    # newline="\n": the files keep Git's line endings on every platform (model.bim must match the generator byte for byte)
    (out / "config" / "employers.yaml").write_text(EMPLOYERS, encoding="utf-8", newline="\n")
    (out / "LICENSE").write_text(MIT.format(owner=owner), encoding="utf-8", newline="\n")
    model = out / MODEL
    model.write_text(neutral_folder(model.read_bytes().decode("utf-8")), encoding="utf-8", newline="\n")


def manifest(out: Path, commit: str) -> dict:
    files = {p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(out.rglob("*")) if p.is_file() and ".git" not in p.relative_to(out).parts}
    return {"source_commit": commit, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "neutral_power_bi_folder": NEUTRAL_FOLDER, "files": files}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the public snapshot of CareerOS (no history, nothing published)")
    parser.add_argument("out", type=Path, help="a new, empty folder outside this repository")
    parser.add_argument("--owner", required=True, help="the copyright holder named in LICENSE")
    parser.add_argument("--commit", default="HEAD")
    args = parser.parse_args(argv)
    out = args.out.resolve()
    if out == ROOT or ROOT in out.parents or out in ROOT.parents:
        parser.error("the snapshot must be built outside this repository")
    if out.exists() and any(out.iterdir()):
        parser.error(f"{out} is not empty")
    commit = git("rev-parse", args.commit).decode().strip()
    out.mkdir(parents=True, exist_ok=True)
    written = export(commit, out)
    replace(out, args.owner)
    record = manifest(out, commit)
    target = ROOT / "release" / f"snapshot-{record['built_at'][:10]}-{commit[:7]}.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    print(f"snapshot of {commit[:7]}: {len(written)} files exported, {len(record['files'])} after replacements, in {out}")
    print(f"manifest: {target.relative_to(ROOT)} (private; never copied into the snapshot)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
