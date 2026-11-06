"""Render README terminal SVGs without signing in or running a review.

Requires the optional documentation dependency: python -m pip install rich
Run from any directory: python scripts/render_terminal_previews.py
"""
from __future__ import annotations

import io
import subprocess
from pathlib import Path

from rich.console import Console
from rich.terminal_theme import TerminalTheme
from rich.text import Text

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'docs' / 'assets'
THEME = TerminalTheme(
    (13, 17, 23), (216, 225, 237),
    [(13, 17, 23), (255, 107, 119), (91, 214, 158), (244, 198, 95),
     (105, 167, 255), (190, 148, 255), (102, 217, 232), (216, 225, 237)],
    [(99, 112, 132), (255, 139, 148), (132, 230, 181), (255, 217, 128),
     (143, 190, 255), (211, 178, 255), (145, 231, 241), (255, 255, 255)],
)


def save(name: str, title: str, output: str, width: int = 106) -> None:
    console = Console(record=True, width=width, file=io.StringIO())
    console.print(Text.from_ansi(output.replace('\r\x1b[K', '')), end='')
    path = OUTPUT / name
    console.save_svg(str(path), title=title, theme=THEME, unique_id=name[:-4])
    path.write_text('\n'.join(line.rstrip() for line in path.read_text().splitlines()) + '\n')


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    script = (ROOT / 'scripts' / 'shipd.sh').read_text()
    # Only load declarations before CLI argument parsing. Never source the full
    # launcher: its main function starts a live workflow.
    marker = '\nwhile [[ $# -gt 0 ]]; do\n'
    if script.count(marker) != 1:
        raise RuntimeError('CLI declaration boundary changed; inspect before rendering')
    declarations = script.split(marker)[0]
    # Enable the existing ANSI renderer when capturing to a pipe.
    declarations = declarations.replace('[[ -t 1 ]]', 'true', 1)
    demo = r'''
init_ui
LOG_FILE=""
ui_header "Shipd Agent · offline UI preview"
ui_review_banner 1 1
ui_phase_update browser done
ui_phase_update auth done
ui_phase_update clock_in done
ui_phase_update reserve done
ui_phase_update clone done
ui_activity_line "12:00:00" phase0 "Example: base PASS · new FAIL · solution PASS"
ui_activity_line "12:00:01" review "Example: collecting evidence for phases 1–6"
ui_phase_update review done
ui_phase_update submit "skip:no-submit"
ui_phase_update cleanup "skip:no-cleanup"
_emit_terminal ""
_emit_terminal "${C_DIM}Example statuses only. No account, evaluation, or API calls.${C_RESET}"
'''
    result = subprocess.run(['bash', '-c', declarations + demo], cwd=ROOT,
                            check=True, capture_output=True, text=True)
    save('terminal-review.svg', 'shipd-agent / workflow preview', result.stdout)
    help_result = subprocess.run(['bash', str(ROOT / 'run.sh'), '--help'], cwd=ROOT,
                                 check=True, capture_output=True, text=True)
    excerpt = '\n'.join(help_result.stdout.splitlines()[:32]) + '\n\n… run ./run.sh --help for the full reference.\n'
    save('terminal-help.svg', 'shipd-agent / CLI help', excerpt)


if __name__ == '__main__':
    main()
