"""
日経平均高配当株50・日経累進高配当株指数の構成銘柄チェック

毎月1回 GitHub Actions から実行する想定。
  1. 日経の指数サイトから2つの指数の構成銘柄を取得
  2. 銘柄数がおかしければ更新せずに「要確認」を通知
  3. 前回のCSVと比べて、入った銘柄・外れた銘柄を出す
  4. 外れた銘柄の理由を自動でタグ付けして卒業組(graduates.csv)に追加
  5. CSVを更新し、Discordに通知(コミットはワークフロー側で行う)

環境変数:
  DISCORD_WEBHOOK_URL  … 通知先(未設定なら標準出力のみ)
  DATA_DIR             … データの置き場所(既定: リポジトリ直下の data)
"""

from __future__ import annotations

import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
MEMBERS_DIR = DATA_DIR / "members"
HISTORY_CSV = DATA_DIR / "members_history.csv"
GRADUATES_CSV = DATA_DIR / "graduates.csv"
LAST_CHECKED = DATA_DIR / "last_checked.txt"

COMPONENT_URL = "https://indexes.nikkei.co.jp/nkave/index/component?idx={idx}"
USER_AGENT = "Mozilla/5.0 (compatible; index-member-checker/1.0)"


@dataclass(frozen=True)
class IndexDef:
    idx: str          # 日経サイトの指数コード
    name: str         # 表示名
    min_count: int    # これより少なければ取得失敗とみなす
    max_count: int


INDEXES = [
    # 臨時除外で50を下回ることがあるが、45未満になる前に補充されるルール
    IndexDef("nk225hdy", "日経平均高配当株50", 44, 50),
    IndexDef("nkphd", "日経累進高配当株指数", 25, 35),
]
N225 = IndexDef("nk225", "日経平均株価", 220, 225)  # 外れた理由の判定用

# 外れた理由の判定に使うしきい値
DIVIDEND_CUT_RATIO = 0.99   # 直近1年の配当が前の1年の99%未満なら減配
PRICE_UP_RATIO = 0.15       # 1年で15%以上上がっていたら株価上昇

REASON_LABELS = {
    "dividend_cut": "減配",
    "n225_removed": "日経225から除外",
    "price_up": "株価上昇で利回り低下",
    "pushed_out": "他銘柄に押し出された",
    "unknown": "理由不明(要確認)",
}
DEFAULT_JUDGMENT = {
    "dividend_cut": "売却検討",
    "n225_removed": "様子見",
    "price_up": "保有継続",
    "pushed_out": "保有継続",
    "unknown": "要確認",
}

MEMBER_FIELDS = ["code", "ticker", "name"]
HISTORY_FIELDS = ["date", "index_name", "change", "code", "ticker", "name"]
GRADUATE_FIELDS = ["ticker", "name", "index_name", "exit_date", "reason_code",
                   "reason", "judgment", "account", "memo"]


class CheckError(Exception):
    """取得や検証の失敗。CSVは更新しない。"""


# ---------------------------------------------------------------- 取得

def fetch_html(url: str, retries: int = 3) -> str:
    last_err: Exception | None = None
    for i in range(retries):
        try:
            res = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            res.raise_for_status()
            res.encoding = res.apparent_encoding or "utf-8"
            return res.text
        except requests.RequestException as e:
            last_err = e
            time.sleep(5 * (i + 1))
    raise CheckError(f"取得に失敗しました: {url} ({last_err})")


CODE_RE = re.compile(r"^[0-9][0-9A-Z]{3}$")  # 1928 や 285A のような証券コード


def parse_members(html: str) -> dict[str, str]:
    """構成銘柄ページのHTMLから {証券コード: 社名} を取り出す。

    ページは業種ごとの表で「コード / 銘柄名 / 社名」の列が並ぶ形。
    表の細かい作りが変わっても動くよう、先頭のセルが証券コードの行をすべて拾う。
    """
    soup = BeautifulSoup(html, "html.parser")
    members: dict[str, str] = {}
    for tr in soup.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 2 or not CODE_RE.match(cells[0]):
            continue
        name = cells[2] if len(cells) >= 3 and cells[2] else cells[1]
        members[cells[0]] = name
    return members


def fetch_members(index: IndexDef) -> dict[str, str]:
    members = parse_members(fetch_html(COMPONENT_URL.format(idx=index.idx)))
    if not (index.min_count <= len(members) <= index.max_count):
        raise CheckError(
            f"{index.name}の銘柄数が{len(members)}件でした"
            f"(想定 {index.min_count}〜{index.max_count}件)。ページの形が変わった可能性があります"
        )
    return members


# ---------------------------------------------------------------- 外れた理由

def tag_reason(code: str, index: IndexDef, n225_codes: set[str] | None) -> str:
    """外れた理由を推定する。判定の順番は 減配 → 日経225除外 → 株価上昇 → 押し出し。"""
    try:
        import yfinance as yf

        t = yf.Ticker(f"{code}.T")

        divs = t.dividends
        if len(divs):
            import pandas as pd
            now = pd.Timestamp.now(tz=divs.index.tz)
            last12 = divs[divs.index > now - timedelta(days=365)].sum()
            prev12 = divs[(divs.index > now - timedelta(days=730)) &
                          (divs.index <= now - timedelta(days=365))].sum()
            if prev12 > 0 and last12 < prev12 * DIVIDEND_CUT_RATIO:
                return "dividend_cut"

        if index.idx == "nk225hdy" and n225_codes is not None and code not in n225_codes:
            return "n225_removed"

        hist = t.history(period="1y")["Close"].dropna()
        if len(hist) >= 2 and hist.iloc[-1] / hist.iloc[0] - 1 >= PRICE_UP_RATIO:
            return "price_up"
        return "pushed_out"
    except Exception as e:  # yfinance のエラーで全体を止めない
        print(f"[warn] {code} の理由判定に失敗: {e}", file=sys.stderr)
        return "unknown"


# ---------------------------------------------------------------- CSV

def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def append_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    write_csv(path, fields, read_csv(path) + rows)


# ---------------------------------------------------------------- 通知

def notify(text: str) -> None:
    print(text)
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    # Discord の上限は2000文字
    for i in range(0, len(text), 1900):
        requests.post(url, json={"content": text[i:i + 1900]}, timeout=30)


# ---------------------------------------------------------------- 本体

def run() -> str:
    fetched = {ix.idx: fetch_members(ix) for ix in INDEXES}  # どちらか失敗したら何も更新しない

    n225_codes: set[str] | None
    try:
        n225_codes = set(fetch_members(N225))
    except CheckError as e:
        print(f"[warn] 日経225の取得に失敗したので、日経225除外の判定は行いません: {e}", file=sys.stderr)
        n225_codes = None

    lines = [f"📋 構成銘柄チェック {TODAY.isoformat()}"]
    history_rows: list[dict] = []
    graduate_rows: list[dict] = []

    for ix in INDEXES:
        new = fetched[ix.idx]
        path = MEMBERS_DIR / f"{ix.idx}.csv"
        old = {r["code"]: r["name"] for r in read_csv(path)}

        if not old:
            lines.append(f"・{ix.name}: 初回登録 {len(new)}銘柄")
        else:
            added = sorted(set(new) - set(old))
            removed = sorted(set(old) - set(new))
            if not added and not removed:
                lines.append(f"・{ix.name}: 変更なし({len(new)}銘柄)")
            else:
                lines.append(f"・{ix.name}: {len(old)} → {len(new)}銘柄")
                for c in added:
                    lines.append(f"　➕ {c} {new[c]}")
                    history_rows.append({"date": TODAY.isoformat(), "index_name": ix.name,
                                         "change": "in", "code": c, "ticker": f"{c}.T", "name": new[c]})
                for c in removed:
                    reason = tag_reason(c, ix, n225_codes)
                    lines.append(f"　➖ {c} {old[c]}({REASON_LABELS[reason]})")
                    history_rows.append({"date": TODAY.isoformat(), "index_name": ix.name,
                                         "change": "out", "code": c, "ticker": f"{c}.T", "name": old[c]})
                    graduate_rows.append({
                        "ticker": f"{c}.T", "name": old[c], "index_name": ix.name,
                        "exit_date": TODAY.isoformat(), "reason_code": reason,
                        "reason": REASON_LABELS[reason], "judgment": DEFAULT_JUDGMENT[reason],
                        "account": "", "memo": "",
                    })

        write_csv(path, MEMBER_FIELDS,
                  [{"code": c, "ticker": f"{c}.T", "name": n} for c, n in sorted(new.items())])

    if history_rows:
        append_csv(HISTORY_CSV, HISTORY_FIELDS, history_rows)
    if graduate_rows:
        append_csv(GRADUATES_CSV, GRADUATE_FIELDS, graduate_rows)

    union = set().union(*(set(m) for m in fetched.values()))
    lines.append(f"監視対象(重複除く): {len(union)}銘柄")

    # 毎回書き換えてコミットさせる(公開リポジトリの60日停止対策も兼ねる)
    LAST_CHECKED.parent.mkdir(parents=True, exist_ok=True)
    LAST_CHECKED.write_text(datetime.now(JST).isoformat(timespec="seconds") + "\n", encoding="utf-8")

    return "\n".join(lines)


def main() -> int:
    try:
        notify(run())
        return 0
    except CheckError as e:
        notify(f"⚠️ 構成銘柄チェック: 要確認\n{e}\nCSVは更新していません。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
