"""Lint docs and source comments.

Usage:
  slop_check.py FILE...   check whole files
  slop_check.py --hook    check the text an Edit, MultiEdit or Write tool call wrote (Claude Code PostToolUse JSON on stdin)

Exit status 1 (file mode) or 2 (hook mode) when a rule fails; each finding names the fix.
"""

import json
import os
import re
import sys
from pathlib import Path

DOC_SKIP_NAMES = {"CLAUDE.md", "AGENTS.md", "SKILL.md", "CHANGELOG.md", "LICENSE.md"}
SKIP_PARTS = {"node_modules", "target", "dist", "build", ".git", ".claude", "archive", "notes", "proposals", "memory", "vendor"}

PERSON = re.compile(r"\b(you|your|yours|yourself|we|our|ours|my)\b|\bus\b", re.IGNORECASE)
MARKETING = re.compile(
    r"\b(lightweight|blazing(ly)?|lightning[- ]fast|seamless(ly)?|effortless(ly)?|powerful|simply|easily|"
    r"out of the box|just works|game[- ]chang\w*|cutting[- ]edge|best[- ]in[- ]class|delightful|"
    r"no accounts|no telemetry|privacy[- ]first|stays on your machine|zero[- ]config)\b",
    re.IGNORECASE,
)
REASSURANCE = re.compile(r"\b(never|nothing else|only talks|calls no|does not send|is not sent|no data)\b", re.IGNORECASE)
PLACEHOLDER = re.compile(r"\b(TBD|TODO|FIXME|coming soon|not yet chosen|to be decided|lorem ipsum|WIP)\b", re.IGNORECASE)

SLASH_LANGS = {".rs", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".swift", ".go", ".c", ".h", ".cc", ".cpp", ".java", ".kt"}
BLOCK_LANGS = SLASH_LANGS | {".css", ".scss"}
HASH_LANGS = {".py", ".sh", ".bash", ".zsh", ".rb"}
HASH_ALLOWED = re.compile(r"#!|#\s*(noqa|type:|shellcheck|pyright|-\*-)")
INSTRUCTION_NAMES = {"CLAUDE.md", "AGENTS.md", "RTK.md"}
PROVENANCE = re.compile(r"\b20\d\d-\d\d-\d\d\b|\bat (my|the user's|the owner's) request\b|\b(owner|user) (said|asked)\b", re.IGNORECASE)


def is_doc(path: Path) -> bool:
    if path.suffix.lower() != ".md" or path.name in DOC_SKIP_NAMES or "prompt" in path.name.lower():
        return False
    if SKIP_PARTS & set(path.parts):
        return False
    return path.name.lower().startswith("readme") or "docs" in path.parts


def lint_instructions(lines: list[str], first: int) -> list[tuple[int, str, str]]:
    fix = "State the rule only; drop dates and who asked for it."
    return [(first + i, "provenance", f"'{m.group(0)}': {fix}") for i, line in enumerate(strip_code(lines)) if (m := PROVENANCE.search(line))]


def is_code(path: Path) -> bool:
    if SKIP_PARTS & set(path.parts):
        return False
    return path.suffix.lower() in BLOCK_LANGS | HASH_LANGS


def strip_code(lines: list[str]) -> list[str]:
    out, fenced = [], False
    for line in lines:
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append("")
            continue
        out.append("" if fenced else re.sub(r"`[^`]*`|\]\([^)]*\)|<[^>]+>", " ", line))
    return out


def lint_doc(lines: list[str], first: int) -> list[tuple[int, str, str]]:
    found, in_limits = [], False
    for i, line in enumerate(strip_code(lines)):
        if line.startswith("#"):
            in_limits = "limit" in line.lower()
        checks = [
            (PERSON, "person", "Write in the imperative or third person; drop you/your/we/our/my."),
            (MARKETING, "marketing", "State what it does; drop the sales word."),
            (PLACEHOLDER, "placeholder", "Remove the placeholder; omit anything undecided."),
        ]
        if not in_limits:
            checks.append((REASSURANCE, "reassurance", "State what the part does instead of what it never does."))
        for rx, rule, fix in checks:
            m = rx.search(line)
            if m and not (rule == "person" and m.group(0) == "US"):
                found.append((first + i, rule, f"'{m.group(0)}': {fix}"))
    return found


def comment_start(line: str, ext: str) -> tuple[int, str] | None:
    quote = None
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c == "\\":
            i += 2
            continue
        elif c == '"' or (c in "'`" and ext in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".sh", ".css"}):
            quote = c
        elif ext in SLASH_LANGS and line.startswith("//", i):
            return i, "//"
        elif ext in BLOCK_LANGS and line.startswith("/*", i):
            return i, "/*"
        elif ext in HASH_LANGS and c == "#":
            prev = line[i - 1] if i else " "
            if prev.isspace() and not (ext == ".sh" and line[i - 2 : i] in {"${", "$#"}):
                return i, "#"
        i += 1
    return None


def lint_code(lines: list[str], ext: str, first: int) -> list[tuple[int, str, str]]:
    found, in_block, module_doc_lines, in_triple = [], False, 0, False
    fix = "Delete the comment. Put meaning in names and types; put design reasons in docs/."
    for i, line in enumerate(lines):
        if in_block:
            if "*/" in line:
                in_block = False
            continue
        if ext == ".py":
            was_in_triple = in_triple
            if (line.count('"""') + line.count("'''")) % 2:
                in_triple = not in_triple
            if was_in_triple:
                continue
        hit = comment_start(line, ext)
        if not hit:
            continue
        col, kind = hit
        text = line[col:].strip()
        if kind == "#" and (HASH_ALLOWED.match(text) or (i == 0 and first == 1 and text.startswith("#!"))):
            continue
        if kind == "//":
            if re.match(r"//\s*(SAFETY:|swift-tools-version:)", text):
                continue
            if text.startswith("//!") and ext == ".rs":
                module_doc_lines += 1
                if module_doc_lines == 1:
                    continue
                found.append((first + i, "comment", "Keep the module doc to one //! line."))
                continue
            if re.search(r"(eslint|swiftlint|biome)-disable|@ts-(ignore|expect-error|nocheck)", text):
                found.append((first + i, "suppression", "Fix the root cause the linter names instead of disabling it."))
                continue
        if kind == "/*" and "*/" not in line[col + 2 :]:
            in_block = True
        found.append((first + i, "comment", f"'{text[:60]}': {fix}"))
    return found


def private_patterns(path: Path) -> list[re.Pattern[str]]:
    cfg = Path.home() / ".config" / "slop-check"
    try:
        roots = [Path(os.path.expanduser(r.strip())).resolve() for r in (cfg / "public-repos.txt").read_text().splitlines() if r.strip()]
        words = [w.strip() for w in (cfg / "private.txt").read_text().splitlines() if w.strip() and not w.startswith("#")]
    except OSError:
        return []
    resolved = path.resolve()
    if not any(resolved.is_relative_to(r) for r in roots):
        return []
    return [re.compile(re.escape(w), re.IGNORECASE) for w in words]


def lint(path: Path, text: str, first: int = 1, unchanged: frozenset[str] = frozenset()) -> list[str]:
    lines = text.splitlines()
    found = []
    if path.name in INSTRUCTION_NAMES:
        found += lint_instructions(lines, first)
    elif is_doc(path):
        found += lint_doc(lines, first)
    elif is_code(path):
        found += lint_code(lines, path.suffix.lower(), first)
    for rx in private_patterns(path):
        for i, line in enumerate(lines):
            if rx.search(line):
                found.append((first + i, "private", "Remove the author's name, path or private project from this public repo."))
    return [f"{path}:{n}: {rule}: {msg}" for n, rule, msg in found if lines[n - first].strip() not in unchanged]


def stripped_lines(text: str) -> frozenset[str]:
    return frozenset(line.strip() for line in text.splitlines())


def hook() -> int:
    data = json.load(sys.stdin)
    inp = data.get("tool_input", {})
    path = Path(inp.get("file_path", ""))
    if not path.name:
        return 0
    if "content" in inp:
        response = data.get("tool_response") or {}
        before = response.get("originalFile") if isinstance(response, dict) else None
        pairs = [(inp["content"], before or "")]
    elif "edits" in inp:
        pairs = [(e.get("new_string", ""), e.get("old_string", "")) for e in inp["edits"]]
    else:
        pairs = [(inp.get("new_string", ""), inp.get("old_string", ""))]
    findings = [f for new, old in pairs for f in lint(path, new, unchanged=stripped_lines(old))]
    if findings:
        print("slop-check: the text just written breaks the docs/comment rules. Fix each line:", file=sys.stderr)
        print("\n".join(findings), file=sys.stderr)
        return 2
    return 0


def main() -> int:
    if sys.argv[1:] == ["--hook"]:
        return hook()
    findings = []
    for name in sys.argv[1:]:
        p = Path(name)
        if p.is_file():
            findings += lint(p, p.read_text(errors="replace"))
    print("\n".join(findings))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
