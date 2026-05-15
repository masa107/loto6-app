#!/usr/bin/env python3
"""
EMBEDDED_DATA 自動更新スクリプト

楽天宝くじのバックナンバーページからロト6/ロト7の最新当選番号を取得し、
loto6.html / loto7.html の EMBEDDED_DATA を25回分（既存件数）を維持しながら
更新する。

- 取得失敗 → 既存ファイルを書き換えず終了
- 既に最新 → 書き換えず終了
"""
import argparse
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup


RAKUTEN_URLS = {
    "loto6": "https://takarakuji.rakuten.co.jp/backnumber/loto6/lastresults/",
    "loto7": "https://takarakuji.rakuten.co.jp/backnumber/loto7/lastresults/",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}


def fetch_results(kind):
    """楽天宝くじから当選番号一覧を取得し、新しい順のリストで返す。"""
    url = RAKUTEN_URLS[kind]
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")

    seen = set()
    results = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if not cells:
                continue
            row_text = " ".join(cells)
            if "第" not in row_text or "回" not in row_text:
                continue
            parsed = parse_row(row_text, kind)
            if parsed and parsed["round"] not in seen:
                seen.add(parsed["round"])
                results.append(parsed)
    results.sort(key=lambda x: -x["round"])
    return results


def parse_row(text, kind):
    rm = re.search(r"第\s*0*(\d+)\s*回", text)
    dm = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", text)
    if not rm or not dm:
        return None
    round_num = int(rm.group(1))
    date = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
    after_date = text[dm.end():]
    nums = [int(x) for x in re.findall(r"\d+", after_date)]
    if kind == "loto6":
        if len(nums) < 7:
            return None
        main = sorted(nums[:6])
        bonus = nums[6]
        if not (all(1 <= n <= 43 for n in main) and 1 <= bonus <= 43):
            return None
        if len(set(main)) != 6 or bonus in main:
            return None
        return {"round": round_num, "date": date, "numbers": main, "bonus": bonus}
    # loto7
    if len(nums) < 9:
        return None
    main = sorted(nums[:7])
    bonus = sorted(nums[7:9])
    if not (all(1 <= n <= 37 for n in main) and all(1 <= n <= 37 for n in bonus)):
        return None
    if len(set(main)) != 7 or len(set(bonus)) != 2:
        return None
    if any(b in main for b in bonus):
        return None
    return {"round": round_num, "date": date, "numbers": main, "bonus": bonus}


def find_block(lines):
    """EMBEDDED_DATA = [ ... ]; の中身の行範囲 (start, end) を返す。"""
    start = end = None
    for i, line in enumerate(lines):
        if start is None and "const EMBEDDED_DATA = [" in line:
            start = i + 1
        elif start is not None and re.match(r"^\s*\];", line):
            end = i
            break
    return start, end


def parse_existing(lines, start, end, kind):
    entries = []
    pattern = re.compile(
        r"round:\s*(\d+),\s*date:\s*\"([^\"]+)\",\s*"
        r"numbers:\s*\[([^\]]+)\],\s*bonus:\s*(.+)"
    )
    for line in lines[start:end]:
        m = pattern.search(line)
        if not m:
            continue
        nums = [int(x.strip()) for x in m.group(3).split(",")]
        bonus_raw = m.group(4)
        if kind == "loto6":
            mb = re.search(r"\d+", bonus_raw)
            if not mb:
                continue
            bonus = int(mb.group(0))
        else:
            bb = re.findall(r"\d+", bonus_raw)
            if len(bb) < 2:
                continue
            bonus = [int(bb[0]), int(bb[1])]
        entries.append({
            "round": int(m.group(1)),
            "date": m.group(2),
            "numbers": nums,
            "bonus": bonus,
        })
    return entries


def format_entry(e, kind):
    nums = ", ".join(str(n) for n in e["numbers"])
    if kind == "loto6":
        return (
            f'  {{ round: {e["round"]}, date: "{e["date"]}", '
            f'numbers: [{nums}], bonus: {e["bonus"]} }},'
        )
    bonus = "[" + ", ".join(str(n) for n in e["bonus"]) + "]"
    return (
        f'  {{ round: {e["round"]}, date: "{e["date"]}", '
        f'numbers: [{nums}], bonus: {bonus} }},'
    )


def update_file(path, kind):
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    start, end = find_block(lines)
    if start is None or end is None:
        print(f"[{kind}] EMBEDDED_DATA ブロックが見つかりません。スキップ。")
        return False
    existing = parse_existing(lines, start, end, kind)
    if not existing:
        print(f"[{kind}] 既存データのパースに失敗。スキップ。")
        return False
    try:
        fetched = fetch_results(kind)
    except Exception as ex:
        print(f"[{kind}] 取得失敗: {ex}", file=sys.stderr)
        return False
    if not fetched:
        print(f"[{kind}] 取得結果が空。スキップ。")
        return False

    latest_existing = existing[0]["round"]
    new_entries = [e for e in fetched if e["round"] > latest_existing]
    if not new_entries:
        print(f"[{kind}] 既に最新です（最新 = 第{latest_existing}回）。")
        return False

    new_entries.sort(key=lambda x: -x["round"])
    target_len = len(existing)
    combined = (new_entries + existing)[:target_len]

    new_data_lines = [format_entry(e, kind) for e in combined]
    new_lines = lines[:start] + new_data_lines + lines[end:]
    new_content = "\n".join(new_lines)
    if content.endswith("\n") or content.endswith("\r\n"):
        new_content += "\n"

    path.write_text(new_content, encoding="utf-8", newline="\n")
    added = [e["round"] for e in new_entries]
    print(f"[{kind}] 追加した回: {added} / 末尾削除して{target_len}件維持")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["loto6", "loto7"], required=True)
    ap.add_argument("--file", required=True)
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"ファイルが見つかりません: {path}", file=sys.stderr)
        return

    changed = update_file(path, args.kind)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")


if __name__ == "__main__":
    main()
