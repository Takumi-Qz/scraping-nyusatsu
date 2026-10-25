"""調達ポータルから公共工事の入札公告を収集し、Googleスプレッドシートに出力する。"""

import argparse
import csv
import datetime as dt
import os
import re
import sys
import time
import unicodedata

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

BASE = "https://www.p-portal.go.jp/pps-web-biz"
SEARCH_PAGE = BASE + "/UAA01/OAA0100?OAA0115"
SEARCH_POST = BASE + "/UAA01/OAA0100"
PAGE_URL = BASE + "/UAA01/OAA0106?page={page}&size=100"
DETAIL_URL = BASE + "/UAA01/OAA0104?procurementItemInfoId={id}"
FORM_ID = "tri_WAA0101FM01"
SLEEP = 1.0
DAYS = int(os.getenv("DAYS", "120"))  # 公開開始日を何日前まで遡るか

# 03=簡易な公共事業（工事）
CLA_CONSTRUCTION = "03"
# 一般競争(WTO) / 指名競争(WTO) / 一般競争(WTO対象外) / 指名競争(WTO対象外)
NOTICE_TYPES = ("05", "06", "10", "12")

HEADER = ["公告日", "入札日", "発注者", "工事名", "工事種別", "地域", "入札方式", "予定価格", "URL"]

# 判定順に意味がある（「道路照明」は電気、「庁舎電気設備改修」は電気、を優先させる）
CATEGORIES = [
    # 工事ではない設計・調査等。工事業者の営業対象から外せるよう最優先で判定
    ("業務委託", ["業務", "調査", "実施設計", "検討"]),
    # 先頭が仕様のキーワード、以降は実データで漏れが多かったものを追加
    ("電気", ["電気設備", "受変電", "照明",
              "受配電", "発電", "電源", "キュービクル", "分電盤", "ケーブル", "電話設備", "灯火", "線灯", "路灯"]),
    ("管", ["下水道", "水道", "配管", "管路",
            "配水管", "給水", "給湯", "排水", "空調", "冷凍機", "衛生設備", "機械設備", "AC更新"]),
    ("土木", ["道路", "河川", "橋梁", "造成", "砂防", "港湾",
              "舗装", "法面", "護岸", "防波堤", "岸壁", "滑走路", "誘導路", "駐車場整備"]),
    ("建築", ["庁舎", "校舎", "住宅", "改修", "新築", "建物",
              "新営", "模様替", "解体", "防水", "修繕", "営繕", "新設工事", "建築"]),
]

BID_DATE_KEYS = r"(?:開札の日時|開札日時|開札日|開札の日|入札執行日時|入札日時|入札の日時|入札書の提出期限|入札書の受領期限)"
DATE_RE = r"(令和|R)?\s*(\d{1,4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"


def classify(name):
    for category, words in CATEGORIES:
        if any(w in name for w in words):
            return category
    return "その他"


def normalize(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip()


def parse_date(text):
    """'令和8年11月24日' / '2026年11月24日' → '2026/11/24'。"""
    m = re.search(DATE_RE, text)
    if not m:
        return ""
    era, y, mo, d = m.groups()
    year = int(y)
    if era or year < 100:
        year += 2018
    try:
        return dt.date(year, int(mo), int(d)).strftime("%Y/%m/%d")
    except ValueError:
        return ""


def serialize_form(form, skip=()):
    """ブラウザ送信と同じ形でフォーム値を集める。"""
    data = []
    for el in form.find_all(["input", "select"]):
        name = el.get("name")
        typ = (el.get("type") or "").lower()
        if not name or name in skip or el.has_attr("disabled") or typ in ("submit", "button"):
            continue
        if typ in ("checkbox", "radio") and not el.has_attr("checked"):
            continue
        if el.name == "select":
            opt = el.find("option", selected=True) or el.find("option")
            data.append((name, opt.get("value", "") if opt else ""))
        else:
            data.append((name, el.get("value", "")))
    return data


def get_html(session, method, url, **kw):
    res = session.request(method, url, timeout=30, **kw)
    res.raise_for_status()
    res.encoding = "utf-8"  # ヘッダはUTF-8だが念のため明示
    return BeautifulSoup(res.text, "lxml")


def parse_rows(soup):
    items = []
    for tr in soup.select("tr.highlight"):
        def cell(key):
            td = tr.find("td", id=re.compile(key + "$"))
            return normalize(td.get_text(" ")) if td else ""

        notice = tr.find("td", id=re.compile("procurementImplementNoticeBean$"))
        if not notice:
            continue
        notice_text = normalize(notice.get_text(" "))
        if "取消" in notice_text:
            continue
        m = re.search(r"procurementItemInfoId', value:'(\d+)'", str(notice))
        if not m:
            continue
        items.append({
            "id": m.group(1),
            "公告日": parse_date(notice_text),
            "発注者": cell("procurementOrgan"),
            "工事名": cell("articleNm"),
            "地域": cell("receiptAddress"),
            "URL": DETAIL_URL.format(id=m.group(1)),
        })
    return items


def search(session, keyword=None):
    """検索→公開開始日降順・100件表示に並び替え→全ページ取得。"""
    soup = get_html(session, "GET", SEARCH_PAGE)
    form = soup.find("form", id=FORM_ID)
    since = (dt.date.today() - dt.timedelta(days=DAYS)).strftime("%Y/%m/%d")
    data = serialize_form(form, skip=("searchConditionBean.cla",
                                      "searchConditionBean.publicStartDateFrom",
                                      "searchConditionBean.articleNm"))
    data += [("searchConditionBean.cla", CLA_CONSTRUCTION),
             ("searchConditionBean.publicStartDateFrom", since),
             ("searchConditionBean.articleNm", keyword or "")]
    data += [("searchConditionBean.procurementClaBean.procurementImplementNotice", v) for v in NOTICE_TYPES]
    data.append(("OAA0102", "検索"))
    time.sleep(SLEEP)
    soup = get_html(session, "POST", SEARCH_POST, data=data)

    count = soup.select_one("p.result-number strong")
    total = int(count.get_text(strip=True).replace(",", "")) if count else 0
    print(f"検索結果: {total}件（公開開始日 {since} 以降）")
    if not total:
        return []

    form = soup.find("form", id=FORM_ID)
    data = serialize_form(form, skip=("sortConditionBean.koumokuName",
                                      "sortConditionBean.order",
                                      "sortConditionBean.displayNo"))
    data += [("sortConditionBean.koumokuName", "06"),  # 公開開始日(調達実施案件公示)
             ("sortConditionBean.order", "02"),        # 降順
             ("sortConditionBean.displayNo", "03"),    # 100件表示
             ("OAA0103", "並び替え")]
    time.sleep(SLEEP)
    soup = get_html(session, "POST", SEARCH_POST, data=data)
    items = parse_rows(soup)

    page = 1
    while page * 100 < total:
        time.sleep(SLEEP)
        try:
            rows = parse_rows(get_html(session, "GET", PAGE_URL.format(page=page)))
        except requests.RequestException as e:
            print(f"  ページ{page + 1} 取得失敗: {e}")
            rows = []
        print(f"  ページ{page + 1}: {len(rows)}件")
        items += rows
        page += 1
    return items


def parse_detail(soup):
    text = normalize(soup.get_text(" "))
    kind = ""
    m = re.search(r"調達種別 (.+?) 分類", text)
    if m:
        kind = m.group(1)
    method = "指名競争入札" if "指名" in kind else "一般競争入札" if "一般" in kind else kind
    if "総合評価" in text:
        method += "（総合評価）"

    bid_date = ""
    for m in re.finditer(BID_DATE_KEYS, text):
        bid_date = parse_date(text[m.end():m.end() + 40])
        if bid_date:
            break

    price = ""
    m = re.search(r"予定価格(?:\(税込\)|\(税抜\))?\s*[:：は]?\s*(?:金)?\s*([\d,]{4,})\s*円", text)
    if m:
        price = m.group(1) + "円"
    return {"入札日": bid_date, "入札方式": method, "予定価格": price}


def fetch_bids(keyword=None):
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (nyusatsu-collector)"
    try:
        items = search(session, keyword)
    except (requests.RequestException, AttributeError) as e:
        print(f"検索失敗: {e}")
        return [], 0

    bids, seen, failed = [], set(), 0
    for i, item in enumerate(items, 1):
        if item["URL"] in seen:
            continue
        seen.add(item["URL"])
        time.sleep(SLEEP)
        try:
            item.update(parse_detail(get_html(session, "GET", item["URL"])))
        except requests.RequestException as e:
            failed += 1
            print(f"  詳細取得失敗 {item['URL']}: {e}")
            item.update({"入札日": "", "入札方式": "", "予定価格": ""})
        item["工事種別"] = classify(item["工事名"])
        bids.append(item)
        if i % 50 == 0:
            print(f"  詳細 {i}/{len(items)}")
    return bids, failed


def sort_bids(bids):
    """入札日が近い順。今日以降→日付不明→過去の順。"""
    today = dt.date.today().strftime("%Y/%m/%d")

    def key(b):
        d = b["入札日"]
        if d and d >= today:
            return (0, d)
        if not d:
            return (1, "")  # 安定ソートなので公告の新しい順のまま
        return (2, d)

    return sorted(bids, key=key)


def get_sheet():
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        os.environ["SERVICE_ACCOUNT_JSON"],
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    book = gspread.authorize(creds).open_by_key(os.environ["SPREADSHEET_ID"])
    name = os.getenv("SHEET_NAME", "入札公告")
    try:
        sheet = book.worksheet(name)
    except gspread.WorksheetNotFound:
        sheet = book.add_worksheet(title=name, rows=1000, cols=len(HEADER))
        sheet.append_row(HEADER)
        sheet.freeze(rows=1)
    if not sheet.row_values(1):
        sheet.append_row(HEADER)
    return sheet


def write_csv(bids, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows([[b[h] for h in HEADER] for b in bids])
    print(f"CSV出力: {path}（{len(bids)}件）")


def main():
    parser = argparse.ArgumentParser(description="調達ポータルの工事入札公告を収集する")
    parser.add_argument("keyword", nargs="?", help="工事名の絞り込みキーワード")
    parser.add_argument("--csv", metavar="PATH", help="スプレッドシートではなくCSVに出力する")
    args = parser.parse_args()

    load_dotenv()
    if not args.csv:
        missing = [k for k in ("SPREADSHEET_ID", "SERVICE_ACCOUNT_JSON") if not os.getenv(k)]
        if missing:
            sys.exit(f"環境変数が未設定です: {', '.join(missing)}（.env を作成するか --csv で実行）")
    bids, failed = fetch_bids(args.keyword)
    bids = sort_bids(bids)
    print(f"取得成功: {len(bids) - failed}件 / 詳細取得失敗: {failed}件")

    if args.csv:
        write_csv(bids, args.csv)
        return

    try:
        sheet = get_sheet()
        existing = set(sheet.col_values(HEADER.index("URL") + 1))
        new = [b for b in bids if b["URL"] not in existing]
        if new:
            sheet.append_rows([[b[h] for h in HEADER] for b in new], value_input_option="USER_ENTERED")
        print(f"シート出力: 追加 {len(new)}件 / 重複スキップ {len(bids) - len(new)}件")
    except Exception as e:  # 認証・API系エラーはまとめて表示して終了
        print(f"シート出力失敗: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
