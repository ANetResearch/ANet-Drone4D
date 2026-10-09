"""Simulated reviewer critique of the paper draft (declared in the paper's statement on the use of language models).

Sends the LaTeX of the background, design and evaluation chapters to an OpenAI-compatible chat endpoint and writes the
review to paper/review/. The key is read from the environment only (REVIEW_API_KEY, else DEEPSEEK_API_KEY);
nothing is stored.

    DEEPSEEK_API_KEY=... python tools/paper/review_llm.py [--model deepseek-v4-pro] [--base https://api.deepseek.com]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "paper"
PROMPT = """You are an expert reviewer for CCF Transactions on Pervasive Computing and Interaction (Springer). Review the
following draft chapters (Background and Motivation, System Design, Evaluation) of a systems paper on a 4D world runtime
for coupled decisions of urban drone fleets. The introduction and conclusion are placeholders; ignore them.

Write a rigorous review with: (1) a summary in 3 sentences; (2) the main strengths; (3) the main weaknesses, ordered by
severity, each with a concrete fix; (4) claims that are not supported by the presented evidence, quoting them; (5)
missing experiments or baselines a reviewer would ask for; (6) unclear writing, inconsistent numbers or terminology
between sections; (7) a score from 1 (reject) to 5 (accept) with a one-paragraph justification. Be specific and
critical; do not praise generically."""


def chapters() -> str:
    out = []
    main = (ROOT / "main.tex").read_text()
    out.append(re.search(r"\\abstract\{%(.*?)\}\n", main, re.S).group(1))
    for name in ("background", "f4", "design", "evaluation", "eval_deleg", "eval_micro", "eval_access", "eval_sens",
                 "table_related", "table_consistency", "numbers"):
        p = ROOT / "sections" / f"{name}.tex"
        if p.exists():
            out.append(f"% ---- {name}.tex\n" + p.read_text())
    return "\n\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="deepseek-v4-pro")
    ap.add_argument("--base", default="https://api.deepseek.com")
    a = ap.parse_args()
    key = os.environ.get("REVIEW_API_KEY") or os.environ["DEEPSEEK_API_KEY"]
    body = {"model": a.model, "temperature": 0.3, "max_tokens": 32000, "stream": True,
            "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": chapters()}]}
    req = urllib.request.Request(f"{a.base.rstrip('/')}/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    parts, finish = [], None
    with urllib.request.urlopen(req, timeout=900) as r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            ch = (json.loads(line[5:]).get("choices") or [{}])[0]
            parts.append((ch.get("delta") or {}).get("content") or "")
            finish = ch.get("finish_reason") or finish
    text = "".join(parts)
    if not text.strip():
        raise SystemExit(f"empty review (finish_reason={finish})")
    out = ROOT / "review"
    out.mkdir(exist_ok=True)
    p = out / f"review_{a.model}_{dt.datetime.now():%Y%m%d_%H%M}.md"
    p.write_text(text)
    print(p)
    print(text)


if __name__ == "__main__":
    main()
