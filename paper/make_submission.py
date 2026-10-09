"""Build the journal upload package in paper/submission/: `python3 paper/make_submission.py`.

The package holds one flattened `main.tex` (every \\input expanded), the bibliography (`refs.bib` and the generated
`main.bbl`), the class and style files, and the figures renamed Fig1, Fig2, ... in order of appearance. TikZ figures stay
inline in the source and are also rendered to standalone FigN.pdf files. The package is compiled once to check it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "submission"
TECTONIC = shutil.which("tectonic") or str(Path.home() / ".local/bin/tectonic")


def expand(text: str) -> str:
    def rep(m: re.Match) -> str:
        p = HERE / m.group(1)
        if p.suffix != ".tex":
            p = p.with_suffix(".tex")
        return expand(p.read_text()).rstrip("\n")
    text = re.sub(r"(?m)^[ \t]*%.*\n", "", text)
    return re.sub(r"\\input\{([^}]+)\}", rep, text)


def preamble(text: str) -> str:
    head = text.split(r"\begin{document}")[0]
    keep = [ln for ln in head.splitlines()
            if ln.startswith((r"\usepackage", r"\usetikzlibrary", r"\definecolor", r"\newcommand", r"\providecommand"))]
    return "\n".join(keep)


def run(cmd: list[str], cwd: Path) -> None:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"{' '.join(cmd)} failed:\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    src = expand((HERE / "main.tex").read_text())
    pre = preamble(src)

    figs = list(re.finditer(r"\\begin\{figure\*?\}.*?\\end\{figure\*?\}", src, re.S))
    out, last = [], 0
    for n, m in enumerate(figs, start=1):
        body = m.group(0)
        imgs = re.findall(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}", body)
        for k, img in enumerate(imgs):
            name = f"Fig{n}" + (chr(ord("a") + k) if len(imgs) > 1 else "")
            shutil.copy(HERE / img, OUT / f"{name}.pdf")
            body = body.replace("{" + img + "}", "{" + name + ".pdf}")
        if "tikzpicture" in body and not imgs:
            art = re.sub(r"^\\begin\{figure\*?\}(\[[^\]]*\])?", "", body.split(r"\caption")[0]).replace(r"\centering", "")
            sa = (f"\\documentclass[border=2pt]{{standalone}}\n{pre}\n\\setlength{{\\textwidth}}{{5.15in}}\n"
                  f"\\begin{{document}}\n{art.strip()}\n\\end{{document}}\n")
            (OUT / f"Fig{n}.tex").write_text(sa)
            run([TECTONIC, "-X", "compile", f"Fig{n}.tex"], OUT)
            (OUT / f"Fig{n}.tex").unlink()
        out.append(src[last:m.start()] + body)
        last = m.end()
    out.append(src[last:])
    (OUT / "main.tex").write_text("".join(out))

    for f in ("refs.bib", "sn-jnl.cls", "sn-mathphys-num.bst"):
        shutil.copy(HERE / f, OUT / f)
    run([TECTONIC, "-X", "compile", "--keep-intermediates", "main.tex"], OUT)
    for f in OUT.glob("main.*"):
        if f.suffix not in (".tex", ".bbl", ".pdf"):
            f.unlink()
    print("\n".join(sorted(p.name for p in OUT.iterdir())))


if __name__ == "__main__":
    main()
