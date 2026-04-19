"""
文案匯入腳本（一次性）
把 文案整理/*.md 的 40 張完整版表格 → Supabase public.series

使用方式：
  export SUPABASE_URL=https://xxx.supabase.co
  export SUPABASE_SERVICE_KEY=ey...
  python scripts/import_series.py
"""

import os
import re
import sys
import json
from pathlib import Path

try:
    from supabase import create_client
except ImportError:
    print("請先安裝： pip install supabase", file=sys.stderr)
    sys.exit(1)


SERIES_SLUG_MAP = {
    "上班系列": "office",
    "戀愛系列": "love",
    "日常系列": "daily",
    "學生系列": "student",
    "職場語錄系列": "workplace_quotes",
    "養生系列": "health",
    "慵懶系列": "lazy",
    "心情系列": "mood",
    "療癒系列": "healing",
    "撒嬌索取系列": "coquet",
    "情緒勒索系列": "emotional_blackmail",
    "嗆人反諷系列": "sarcasm",
    "霸道搭訕系列": "domineering",
    "日常回應系列": "reply",
    "台語俗語系列": "taiwanese",
    "文學古風系列": "literary",
}


def parse_md(path: Path):
    """抓『## 40 張完整版』下面那張表。回傳 items list。"""
    text = path.read_text(encoding="utf-8")

    # 抓出 40 張完整版區段（允許標題前有其他文字，如「小蛙心情款 — 40 張完整版」）
    m = re.search(r"##[^\n]*40\s*張完整版[^\n]*\n(.*?)(?:\n##\s|\Z)", text, re.S)
    if not m:
        return []

    section = m.group(1)
    items = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        idx, text_, action = cells[0], cells[1], cells[2]
        # 跳過表頭與分隔列
        if idx in ("編號", ""):
            continue
        if set(idx) <= set("-: "):
            continue
        items.append({"idx": idx, "text": text_, "action": action})
    return items


def main():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("需要環境變數 SUPABASE_URL 和 SUPABASE_SERVICE_KEY", file=sys.stderr)
        sys.exit(1)

    sb = create_client(url, key)

    base = Path(__file__).resolve().parent.parent / "文案整理"
    if not base.exists():
        print(f"找不到資料夾：{base}", file=sys.stderr)
        sys.exit(1)

    rows = []
    for md in sorted(base.glob("*.md")):
        stem = md.stem
        if stem == "總表":
            continue
        slug = SERIES_SLUG_MAP.get(stem)
        if not slug:
            print(f"  跳過未列管的系列：{stem}")
            continue
        items = parse_md(md)
        if not items:
            print(f"  {stem}：表格解析為空，跳過")
            continue
        rows.append({"id": slug, "name": stem, "items": items})
        print(f"  {stem} → {slug} ({len(items)} 條)")

    if not rows:
        print("沒有可匯入的資料")
        return

    # upsert 進 Supabase
    resp = sb.table("series").upsert(rows).execute()
    print(f"\n匯入完成：{len(rows)} 個系列")


if __name__ == "__main__":
    main()
