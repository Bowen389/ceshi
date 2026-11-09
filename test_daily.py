import unittest
from daily import fresh, settle

class PaperTest(unittest.TestCase):
    def test_position_lifecycle(self):
        book = fresh()
        candidates = {"FB_DUAL_K4": [{"code": "600001", "arm": "C2", "amount": 1e8}],
                      "U4": [{"code": "600001", "arm": "U4a", "mean": 3e8}],
                      "V6": [{"code": "600001", "arm": "双臂", "mean": 3e8}]}
        bar = {"eligible": True, "traded": True, "open": 10, "upper": 11,
               "lower": 9, "prev_close": 10, "close": 11, "sealed": True}
        settle(book, candidates, "2026-11-10", {"600001": bar})
        self.assertTrue(all(x["positions"] and not x["trades"] for x in book.values()))
        empty = {key: [] for key in candidates}
        settle(book, empty, "2026-11-11", {"600001": bar})
        self.assertEqual(len(book["FB_DUAL_K4"]["trades"]), 1)
        self.assertEqual(book["U4"]["positions"]["600001"]["extensions"], 1)
        self.assertEqual(book["V6"]["positions"]["600001"]["extensions"], 1)
        settle(book, empty, "2026-11-12", {"600001": dict(bar, close=10, sealed=False)})
        self.assertTrue(all(not x["positions"] and len(x["trades"]) == 1 for x in book.values()))

if __name__ == "__main__":
    unittest.main()
