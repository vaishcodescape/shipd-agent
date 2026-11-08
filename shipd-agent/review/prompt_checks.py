# Deterministic Phase 1 checks on the task prompt: the word limit and an
# estimate of Shipd's "Too Many Requirements" check.

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from review.context import PROBLEM_CANDIDATES, resolve_repo_path
from review.rubric_defaults import MAX_PROMPT_REQUIREMENTS, MAX_PROMPT_WORDS
from review.schemas import Finding, PhaseResult

# A cloned submission's README.md is the target project's own docs, not the
# task prompt, so only explicit prompt files are measured.
TASK_PROMPT_CANDIDATES: tuple[str, ...] = tuple(
    name for name in PROBLEM_CANDIDATES if name.lower() != "readme.md"
)

# Whitespace-delimited tokens containing a letter or digit (bullets, "#" and
# "—" on their own are markup, not words).
_WORD_RE = re.compile(r"\S*[^\W_]\S*")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+(.*\S)")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_MODAL_RE = re.compile(
    r"\b(?:must|shall|should|needs? to|has to|have to|(?:is|are) required to)\b",
    re.I,
)
_IMPERATIVE_RE = re.compile(
    r"^(?:add|implement|support|allow|ensure|make|provide|expose|return|raise|handle|"
    r"update|extend|introduce|create|remove|rename|preserve|keep|validate|reject|emit|"
    r"accept|fix|change|replace|refactor|document|log|record|include|exclude|prevent|"
    r"avoid|use)\b",
    re.I,
)
_TOO_MANY_REQUIREMENTS_RE = re.compile(r"too many requirements", re.I)
_FLAGGED_RE = re.compile(r"\b(?:fail(?:ed)?|flag(?:ged)?|warning)\b|[✗✘❌⚠]", re.I)
_PASSED_RE = re.compile(r"\bpass(?:ed)?\b|[✓✔✅]", re.I)


@dataclass
class PromptAnalysis:
    source: str | None
    word_count: int | None = None
    requirements: list[str] = field(default_factory=list)
    platform_flag: str | None = None
    body: str = ""

    @property
    def requirement_count(self) -> int:
        return len(self.requirements)

    @property
    def requirements_label(self) -> str:
        noun = "requirement" if self.requirement_count == 1 else "requirements"
        return f"~{self.requirement_count} independent {noun}"


def find_task_prompt_path(repo_path: Path) -> Path | None:
    repo_path = resolve_repo_path(repo_path)
    for name in TASK_PROMPT_CANDIDATES:
        candidate = repo_path / name
        if candidate.is_file():
            return candidate
    return None


def count_words(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def _strip_code_fences(text: str) -> list[str]:
    lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append(line)
    return lines


def extract_requirements(text: str) -> list[str]:
    """Independent requirements in a task prompt (heuristic).

    Each list item is at least one requirement; a prose sentence counts once per
    modal ("must", "should", "needs to", …) or once if it opens with an
    imperative verb ("Add …", "Support …"). Code fences and headings are ignored.
    """
    requirements: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        for sentence in _SENTENCE_SPLIT_RE.split(" ".join(paragraph)):
            sentence = sentence.strip()
            if not sentence:
                continue
            modals = len(_MODAL_RE.findall(sentence))
            weight = modals or (1 if _IMPERATIVE_RE.match(sentence) else 0)
            requirements.extend([sentence] * weight)
        paragraph.clear()

    for line in _strip_code_fences(text or ""):
        if not line.strip() or _HEADING_RE.match(line):
            flush_paragraph()
            continue
        item = _LIST_ITEM_RE.match(line)
        if item:
            flush_paragraph()
            body = item.group(1)
            requirements.extend([body] * max(1, len(_MODAL_RE.findall(body))))
            continue
        paragraph.append(line.strip())
    flush_paragraph()
    return requirements


def platform_too_many_requirements_flag(holistic_text: str) -> str | None:
    """The Holistic Check line flagging "Too Many Requirements", if any.

    The status may sit on the same line ("Too Many Requirements: FAIL") or on the
    next non-empty line when the panel renders it as a separate badge.
    """
    lines = [line.strip() for line in (holistic_text or "").splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if not _TOO_MANY_REQUIREMENTS_RE.search(line):
            continue
        remainder = _TOO_MANY_REQUIREMENTS_RE.sub("", line)
        if _FLAGGED_RE.search(remainder):
            return line
        if _PASSED_RE.search(remainder):
            return None
        if index + 1 < len(lines) and _FLAGGED_RE.fullmatch(lines[index + 1]):
            return f"{line} {lines[index + 1]}"
    return None


_EM_DASH_RE = re.compile(r"—|–|(?:\s--\s)")
_URL_RE = re.compile(r"https?://|www\.\w", re.I)
_BLANK_RUN_RE = re.compile(r"(?:[ \t]*\n){4,}")
_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]")


def _external_title(repo_path: Path, prompt_text: str) -> str:
    """A title that is not already inside the prompt file."""
    front = re.match(r"^---\n(.*?)\n---", prompt_text, re.S)
    if front:
        match = re.search(r"^title:\s*[\"']?(.+?)[\"']?\s*$", front.group(1), re.I | re.M)
        if match and match.group(1).strip() in prompt_text:
            return ""
    for name in ("title.txt", "TITLE", "title.md"):
        path = repo_path / name
        if not path.is_file():
            continue
        try:
            title = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if title and title not in prompt_text:
            return title.splitlines()[0].strip()
    return ""


def style_findings(text: str, *, source: str) -> list[Finding]:
    """Precheck and style flags. Category fit stays with the Phase 1 reviewer."""
    findings: list[Finding] = []
    prose = "\n".join(_strip_code_fences(text or ""))
    wrapped = [
        line
        for line in prose.splitlines()
        if 68 <= len(line.rstrip()) <= 88 and line.strip()
    ]
    if len(wrapped) >= 8:
        findings.append(
            Finding(
                phase="1",
                severity="MAJOR",
                finding="Task prompt looks hard-wrapped",
                evidence=f"{source}: {len(wrapped)} lines are between 68 and 88 characters.",
                suggested_fix="Write ordinary paragraphs. Do not hard-wrap near 70 to 85 characters.",
            )
        )
    if len(_EM_DASH_RE.findall(text or "")) >= 4:
        findings.append(
            Finding(
                phase="1",
                severity="MAJOR",
                finding="Task prompt uses frequent em dashes",
                evidence=f"{source}: frequent em dashes or spaced double hyphens.",
                suggested_fix="Prefer commas or full stops. Frequent em dashes read as generated prose.",
            )
        )
    for paragraph in re.split(r"\n\s*\n", prose):
        if count_words(paragraph) >= 120:
            findings.append(
                Finding(
                    phase="1",
                    severity="MAJOR",
                    finding="Task prompt has a wall-of-text paragraph",
                    evidence=f"{source}: a paragraph is at least 120 words.",
                    suggested_fix="Break the paragraph so a reader can see the ask.",
                )
            )
            break
    if _BLANK_RUN_RE.search(text or ""):
        findings.append(
            Finding(
                phase="1",
                severity="MAJOR",
                finding="Task prompt has four or more blank lines in a row",
                evidence=source,
                suggested_fix="Collapse the extra blank lines.",
            )
        )
    non_ascii = [
        char
        for char in _NON_ASCII_RE.findall(text or "")
        if char not in "—–"
    ]
    if non_ascii:
        findings.append(
            Finding(
                phase="1",
                severity="MAJOR",
                finding="Task prompt contains non-ASCII characters",
                evidence=f"{source}: {''.join(sorted(set(non_ascii))[:12])!r}",
                suggested_fix="Keep the prompt in ASCII.",
            )
        )
    if _URL_RE.search(text or ""):
        findings.append(
            Finding(
                phase="1",
                severity="MAJOR",
                finding="Task prompt contains a URL",
                evidence=source,
                suggested_fix="Remove URLs from the prompt.",
            )
        )
    if any(_LIST_ITEM_RE.match(line) for line in prose.splitlines()):
        findings.append(
            Finding(
                phase="1",
                severity="MINOR",
                finding="Task prompt uses a bulleted requirement list",
                evidence=source,
                suggested_fix="State the ask in prose instead of a bullet list of requirements.",
            )
        )
    if any(_HEADING_RE.match(line) for line in (text or "").splitlines()):
        findings.append(
            Finding(
                phase="1",
                severity="MINOR",
                finding="Task prompt uses headings",
                evidence=source,
                suggested_fix="Drop markdown headings. The prompt should read as prose.",
            )
        )
    if _FENCE_RE.search(text or ""):
        findings.append(
            Finding(
                phase="1",
                severity="MINOR",
                finding="Task prompt contains a code snippet",
                evidence=source,
                suggested_fix="Describe the behaviour in prose when a snippet is not required.",
            )
        )
    return findings


def analyze_task_prompt(repo_path: Path, *, holistic_text: str = "") -> PromptAnalysis:
    path = find_task_prompt_path(repo_path)
    flag = platform_too_many_requirements_flag(holistic_text)
    if path is None:
        return PromptAnalysis(source=None, platform_flag=flag)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return PromptAnalysis(source=None, platform_flag=flag)
    repo = resolve_repo_path(repo_path)
    title = _external_title(repo, text)
    word_count = count_words(text) + (count_words(title) if title else 0)
    return PromptAnalysis(
        source=str(path.relative_to(repo)),
        word_count=word_count,
        requirements=extract_requirements(text),
        platform_flag=flag,
        body=text,
    )


def format_prompt_analysis(
    analysis: PromptAnalysis,
    *,
    max_words: int = MAX_PROMPT_WORDS,
    max_requirements: int = MAX_PROMPT_REQUIREMENTS,
) -> str:
    lines: list[str] = []
    if analysis.source is None:
        lines.append(
            "Task prompt file not found in the clone (looked for "
            + ", ".join(TASK_PROMPT_CANDIDATES)
            + f") — check the prompt is ≤ {max_words} words and focused in scope manually."
        )
    else:
        lines.append(
            f"Task prompt ({analysis.source}): {analysis.word_count} words "
            f"(limit {max_words}); {analysis.requirements_label} "
            f"(heuristic flag above {max_requirements})."
        )
    if analysis.platform_flag:
        lines.append(f"Shipd flagged Too Many Requirements: {analysis.platform_flag}")
    return "\n".join(lines)


def evaluate_prompt_phase1(
    analysis: PromptAnalysis,
    *,
    max_words: int = MAX_PROMPT_WORDS,
    max_requirements: int = MAX_PROMPT_REQUIREMENTS,
) -> tuple[PhaseResult | None, list[Finding]]:
    """Word-limit and Too Many Requirements verdict for Phase 1.

    Returns no phase result when there was nothing to measure. A PASS covers only
    these mechanical bars — the rest of Phase 1 (clarity, AI slop, repo fit) is
    still judged by the reviewer.
    """
    findings: list[Finding] = []
    issues: list[str] = []
    source = analysis.source or "task prompt"

    if analysis.word_count is not None and analysis.word_count > max_words:
        findings.append(
            Finding(
                phase="1",
                severity="MAJOR",
                finding=f"Task prompt exceeds the {max_words:,}-word limit",
                evidence=f"{source}: {analysis.word_count} words (limit {max_words})",
                suggested_fix=(
                    f"Trim the prompt to ≤ {max_words:,} words — cut detail that doesn't "
                    "specify the behaviour; extra detail also risks Too Many Requirements."
                ),
            )
        )
        issues.append(f"{analysis.word_count} words > {max_words}")

    if analysis.platform_flag:
        findings.append(
            Finding(
                phase="1",
                severity="MAJOR",
                finding="Shipd flagged Too Many Requirements",
                evidence=(
                    f"Holistic Check: {analysis.platform_flag}"
                    + (
                        f"; {analysis.requirements_label} in {source}"
                        if analysis.source
                        else ""
                    )
                ),
                suggested_fix=(
                    "Narrow the prompt to one focused behavioural ask; drop or merge "
                    "independent requirements."
                ),
            )
        )
        issues.append("Shipd Too Many Requirements flag")
    elif analysis.source and analysis.requirement_count > max_requirements:
        findings.append(
            Finding(
                phase="1",
                severity="MAJOR",
                finding="Too many independent requirements in task prompt",
                evidence=(
                    f"{source}: {analysis.requirements_label} "
                    f"(flag above {max_requirements})"
                ),
                suggested_fix=(
                    "Narrow the prompt to one focused behavioural ask; drop or merge "
                    "independent requirements so it passes Shipd's Too Many Requirements check."
                ),
            )
        )
        issues.append(
            f"~{analysis.requirement_count} requirements > {max_requirements}"
        )

    if analysis.body:
        findings.extend(style_findings(analysis.body, source=source))

    blocking = [item for item in findings if item.severity in {"BLOCKER", "MAJOR"}]
    if blocking:
        return (
            PhaseResult(
                status="FAIL",
                summary="Task prompt checks failed: " + "; ".join(issues or [item.finding for item in blocking]),
            ),
            findings,
        )
    if analysis.source is None and not findings:
        return None, []
    if analysis.source is None:
        return (
            PhaseResult(status="PASS", summary="No task prompt file to measure."),
            findings,
        )
    return (
        PhaseResult(
            status="PASS",
            summary=(
                f"Task prompt within bars: {analysis.word_count}/{max_words} words, "
                f"{analysis.requirements_label}."
            ),
        ),
        findings,
    )
