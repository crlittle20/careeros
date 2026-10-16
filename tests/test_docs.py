"""Architecture, security, lineage and decision documents (Portfolio Prompt 8, 2026-10-13), and since Prompt 11 the
README, case study and AI-assisted development disclosure, stay true to the code.

The documents name many files, tests, tables and commands. These checks fail as soon as one of them no longer exists,
so the documents cannot drift silently:
- every repository path and every cited test exists;
- every table of the database is in data_lineage.md, and every table it names exists;
- every `careeros <command>` is a real command;
- every relative link resolves; every Mermaid block starts with a known diagram type;
- every ADR has its sections and is in the index.
"""
import re
import unittest
from pathlib import Path

from careeros.core.clock import FixedClock
from careeros.db.store import open_store
from careeros.paths import ENGINE_ROOT

DOCS = ENGINE_ROOT / "docs"
CHECKED = [DOCS / "architecture.md", DOCS / "security_architecture.md", DOCS / "data_lineage.md",
           *sorted((DOCS / "adr").glob("*.md")),
           ENGINE_ROOT / "README.md", DOCS / "case_study.md", DOCS / "ai_assisted_development.md",   # Prompt 11
           DOCS / "demo_walkthrough.md", DOCS / "portfolio_mvp_report.md",   # Prompt 12
           DOCS / "experience_match.md", DOCS / "certifications.md"]   # 2026-10-15
DIAGRAMS = ("flowchart", "graph", "sequenceDiagram", "stateDiagram-v2", "erDiagram", "classDiagram")


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def code_spans(path: Path) -> list[str]:
    return re.findall(r"`([^`\n]+)`", text(path))


def tests_defined() -> set[str]:
    names = set()
    for f in (ENGINE_ROOT / "tests").glob("test_*.py"):
        names |= set(re.findall(r"def (test_\w+)\(", text(f)))
    return names


def commands() -> set[str]:
    return set(re.findall(r'sub\.add_parser\("([\w-]+)"', text(ENGINE_ROOT / "careeros" / "cli.py")))


class DocumentsTest(unittest.TestCase):
    def test_every_named_file_exists(self):
        """Paths are written from the repository root, or from careeros/, config/, docs/ or careeros/discovery/; files
        that live in the private vault are named but never in the repository."""
        bases = [ENGINE_ROOT, ENGINE_ROOT / "careeros", ENGINE_ROOT / "config", DOCS, ENGINE_ROOT / "careeros" / "discovery"]
        names = {p.name for p in ENGINE_ROOT.rglob("*") if not any(part.startswith(".venv") for part in p.parts) and ".git" not in p.parts}
        vault_files = {"overrides.yaml", "curated_employers.yaml", "private_sites.yaml"}
        missing = []
        for doc in CHECKED:
            for span in code_spans(doc):
                for token in re.findall(r"[\w./\\-]+\.(?:py|md|yaml|json|sql)\b", span):
                    token = token.replace("\\", "/")
                    found = (token.split("/")[-1] in vault_files
                             or (any((b / token).exists() for b in bases) if "/" in token else token in names))
                    if not found:
                        missing.append(f"{doc.name}: {token}")
        self.assertEqual(missing, [])

    def test_every_cited_test_exists(self):
        """A cited test is a test function, a test module (test_board_verification), or the start of a family of tests
        (test_t01 ... test_t30, test_t13_*)."""
        defined = tests_defined()
        modules = {p.stem for p in (ENGINE_ROOT / "tests").glob("test_*.py")}
        missing = [f"{doc.name}: {name}" for doc in CHECKED for name in re.findall(r"\b(test_\w+)", text(doc))
                   if name not in defined and name not in modules and not any(d.startswith(name) for d in defined)]
        self.assertEqual(missing, [])

    def test_data_lineage_names_every_table_and_only_real_tables(self):
        store = open_store(":memory:", FixedClock("2026-10-13T00:00:00.000Z"))
        tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        store.close()
        documented = set(re.findall(r"^\| `(\w+)` \|", text(DOCS / "data_lineage.md"), re.M))   # the table list's first column
        self.assertEqual(tables - documented, set(), "tables missing from data_lineage.md")
        self.assertEqual(documented - tables, set(), "names in the table list that are not tables")

    def test_every_careeros_command_is_real(self):
        known, unknown = commands(), []
        for doc in CHECKED:
            for name in re.findall(r"`careeros ([\w-]+)", text(doc)):
                if name not in known:
                    unknown.append(f"{doc.name}: careeros {name}")
        self.assertEqual(unknown, [])

    def test_links_resolve_and_diagrams_are_typed(self):
        broken, untyped = [], []
        for doc in CHECKED:
            body = text(doc)
            for target in re.findall(r"\]\(([^)#\s]+)\)", body):
                if not target.startswith("http") and not (doc.parent / target).exists():
                    broken.append(f"{doc.name}: {target}")
            for block in re.findall(r"```mermaid\n(.*?)```", body, re.S):
                if not block.lstrip().startswith(DIAGRAMS):
                    untyped.append(f"{doc.name}: {block[:30]!r}")
        self.assertEqual((broken, untyped), ([], []))

    def test_every_adr_has_its_sections_and_is_indexed(self):
        index = text(DOCS / "adr" / "README.md")
        records = sorted(p for p in (DOCS / "adr").glob("[0-9][0-9][0-9][0-9]-*.md"))
        self.assertGreaterEqual(len(records), 7)
        for record in records:
            body = text(record)
            for part in ("- Status:", "- Decided:", "- Recorded:", "## Context", "## Decision", "## Consequences",
                         "## Alternatives considered", "## Evidence"):
                self.assertIn(part, body, f"{record.name}: {part}")
            self.assertIn(f"({record.name})", index, f"{record.name} is not in adr/README.md")
            self.assertIn(f"adr/{record.name}", text(DOCS / "architecture.md"), f"{record.name} is not in architecture.md")


if __name__ == "__main__":
    unittest.main()
