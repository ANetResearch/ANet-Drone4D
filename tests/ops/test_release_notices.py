"""发布面的数据集授权说明与 ECCV 2022 BibTeX（D2-AC-24；ADR-109 第 3 条；R-D2-28；ADR-079 的发布面范围）。

D2-AC-24 要求 NOTICE、THIRD_PARTY_NOTICES、README 与关于页都含"经数据集权利方授权使用"与 ECCV 2022 BibTeX。第 1 轮验收时 NOTICE
只写"见 THIRD_PARTY_NOTICES 第 4 节"而没有 BibTeX 本身；本用例逐处核对文字，并要求 BibTeX 与前端常量（关于页与世界列表，
`apps/web/src/lib/demo.ts` 的 `URBANSCENE_BIBTEX`）逐字相同，任何一处改写都会使用例失败。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PHRASE = "经数据集权利方授权使用"
BIBTEX = """@inproceedings{UrbanScene3D,
  title     = {Capturing, Reconstructing, and Simulating: the UrbanScene3D Dataset},
  author    = {Liqiang Lin and Yilin Liu and Yue Hu and Xingguang Yan and Ke Xie and Hui Huang},
  booktitle = {ECCV},
  year      = {2022}
}"""
RELEASE_DOCS = ["NOTICE", "THIRD_PARTY_NOTICES.md", "README.md", "README.zh-CN.md"]


@pytest.mark.parametrize("name", RELEASE_DOCS)
def test_release_doc_states_authorization_and_bibtex(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    assert PHRASE in text, f"{name} 缺少授权说明"
    assert BIBTEX in text, f"{name} 缺少 ECCV 2022 BibTeX（或与 THIRD_PARTY_NOTICES 第 4 节不一致）"
    assert "ECCV 2022" in text


def test_notice_keeps_the_scope_of_the_authorization():
    """授权只覆盖本项目自己的部署：NOTICE 仍说明仓库不含数据集、自行下载者受作者条款约束。"""
    text = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "does not include the UrbanScene3D dataset" in text
    assert "not passed on by this" in text and "authors' terms" in text


def test_web_constant_is_the_same_bibtex():
    src = (ROOT / "apps" / "web" / "src" / "lib" / "demo.ts").read_text(encoding="utf-8")
    m = re.search(r"export const URBANSCENE_BIBTEX = `([^`]*)`", src)
    assert m, "apps/web/src/lib/demo.ts 没有 URBANSCENE_BIBTEX"
    assert m.group(1) == BIBTEX


def test_about_page_texts():
    """关于页（AboutContent）显示 about.dataSummary 与 URBANSCENE_BIBTEX；世界面板对六城显示授权说明。"""
    zh = json.loads((ROOT / "apps" / "web" / "src" / "app" / "i18n" / "zh-CN.json").read_text(encoding="utf-8"))
    assert "经数据集权利方授权" in zh["about.dataSummary"] and "科研" not in zh["about.dataSummary"]
    assert PHRASE in zh["world.datasetAuthorized"] and PHRASE in zh["hub.urbanscene"]
    about = (ROOT / "apps" / "web" / "src" / "ui" / "views" / "AboutDialog.tsx").read_text(encoding="utf-8")
    assert "URBANSCENE_BIBTEX" in about and "data-about-bibtex" in about
