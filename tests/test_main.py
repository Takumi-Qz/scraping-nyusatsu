import unittest

from bs4 import BeautifulSoup

from main import classify, parse_date, parse_detail, sort_bids


class ClassifyTest(unittest.TestCase):
    def test_categories(self):
        cases = {
            "国道9号道路改良工事": "土木",
            "一級河川護岸整備工事": "土木",
            "○○橋梁補修工事": "土木",
            "合同庁舎改修工事": "建築",
            "小学校校舎新築工事": "建築",
            "受変電設備更新工事": "電気",
            "大分空港道路照明灯改良その他工事": "電気",  # 道路(土木)より照明(電気)を優先
            "下水道管渠更生工事": "管",
            "配水管布設替工事": "管",
            "庁舎改修工事設計業務": "業務委託",
            "灯浮標交換工事": "その他",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(classify(name), expected)


class ParseDateTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_date("令和８年１１月２４日（火）"), "2026/11/24")  # 全角
        self.assertEqual(parse_date("令和8 年11 月9 日"), "2026/11/09")  # PDF由来の空白
        self.assertEqual(parse_date("2026年1月5日"), "2026/01/05")
        self.assertEqual(parse_date("日付なし"), "")


class ParseDetailTest(unittest.TestCase):
    def test_extracts_bid_date_and_method(self):
        html = """<div>調達種別 一般競争入札の入札公告（WTO対象外） 分類 簡易工事
        公告内容 （８）入札書の提出期限 令和8年11月24日（火）12時00分
        総合評価落札方式 予定価格 金12,345,000円</div>"""
        result = parse_detail(BeautifulSoup(html, "lxml"))
        self.assertEqual(result["入札日"], "2026/11/24")
        self.assertEqual(result["入札方式"], "一般競争入札（総合評価）")
        self.assertEqual(result["予定価格"], "12,345,000円")


class SortTest(unittest.TestCase):
    def test_upcoming_then_unknown_then_past(self):
        bids = [{"入札日": d} for d in ("2000/01/01", "", "2999/01/02", "2999/01/01")]
        self.assertEqual([b["入札日"] for b in sort_bids(bids)],
                         ["2999/01/01", "2999/01/02", "", "2000/01/01"])


if __name__ == "__main__":
    unittest.main()
