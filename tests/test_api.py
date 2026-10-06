import os
import tempfile
import unittest
from fractions import Fraction

os.environ["LIGHTS_DB"] = os.path.join(tempfile.mkdtemp(), "test_api.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.engine import Item, evaluate, resolve  # noqa: E402
from app.main import app  # noqa: E402


def abs_body(fixture="L1", channel=1, target=100, duration=1000, ms=0):
    return {"fixture": fixture, "channel": channel, "target": target,
            "duration": duration, "start_mode": "abs", "start_ms": ms}


def ref_body(ref_id, edge="start", offset=0, fixture="L1", channel=1,
             target=100, duration=1000):
    return {"fixture": fixture, "channel": channel, "target": target,
            "duration": duration, "start_mode": "ref",
            "ref_id": ref_id, "ref_edge": edge, "ref_offset": offset}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        cls.client = TestClient(app)
        cls.client.__enter__()  # reuse one portal/event loop for every request

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        # fresh empty project per test
        r = self.client.post("/api/projects", json={"name": "t"})
        self.pid = r.json()["id"]
        self.base = f"/api/projects/{self.pid}"

    def test_create_and_resolve(self):
        r = self.client.post(f"{self.base}/items", json=abs_body(ms=100, duration=200))
        self.assertEqual(r.status_code, 201, r.text)
        it = r.json()["items"][0]
        self.assertEqual(it["actual_start"], 100)
        self.assertEqual(it["actual_end"], 300)

    def test_missing_reference_rejected_and_unchanged(self):
        self.client.post(f"{self.base}/items", json=abs_body())
        before = self.client.get(self.base).json()
        r = self.client.post(f"{self.base}/items", json=ref_body(999))
        self.assertEqual(r.status_code, 400)
        self.assertIn("引用缺失", r.json()["detail"])
        after = self.client.get(self.base).json()
        self.assertEqual(len(after["items"]), 1)
        self.assertEqual(before, after)

    def test_cycle_rejected(self):
        a = self.client.post(f"{self.base}/items", json=abs_body(channel=1)).json()
        id_a = a["items"][0]["id"]
        b = self.client.post(f"{self.base}/items",
                             json=ref_body(id_a, channel=2)).json()
        id_b = b["items"][-1]["id"]
        r = self.client.put(f"{self.base}/items/{id_a}",
                            json=ref_body(id_b, channel=1))
        self.assertEqual(r.status_code, 400)
        self.assertIn("环", r.json()["detail"])
        # item a still absolute
        cur = self.client.get(self.base).json()["items"]
        self.assertEqual(cur[0]["start_mode"], "abs")

    def test_same_instant_rejected(self):
        self.client.post(f"{self.base}/items", json=abs_body(ms=50))
        r = self.client.post(f"{self.base}/items", json=abs_body(ms=50, target=20))
        self.assertEqual(r.status_code, 400)
        self.assertIn("同时起步", r.json()["detail"])
        self.assertEqual(len(self.client.get(self.base).json()["items"]), 1)

    def test_delete_referenced_rejected(self):
        a = self.client.post(f"{self.base}/items", json=abs_body()).json()
        id_a = a["items"][0]["id"]
        self.client.post(f"{self.base}/items", json=ref_body(id_a, channel=2))
        r = self.client.delete(f"{self.base}/items/{id_a}")
        self.assertEqual(r.status_code, 409)
        self.assertIn("引用", r.json()["detail"])
        self.assertEqual(len(self.client.get(self.base).json()["items"]), 2)

    def test_delete_then_ok(self):
        a = self.client.post(f"{self.base}/items", json=abs_body()).json()
        id_a = a["items"][0]["id"]
        r = self.client.delete(f"{self.base}/items/{id_a}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["items"], [])

    def test_recalculate_after_edit(self):
        a = self.client.post(f"{self.base}/items",
                             json=abs_body(ms=0, duration=100)).json()
        id_a = a["items"][0]["id"]
        b = self.client.post(f"{self.base}/items",
                             json=ref_body(id_a, "end", offset=10, channel=2)).json()
        id_b = b["items"][-1]["id"]
        self.assertEqual(b["items"][-1]["actual_start"], 110)
        r = self.client.put(f"{self.base}/items/{id_a}",
                            json=abs_body(ms=0, duration=250))
        self.assertEqual(r.status_code, 200)
        b_after = next(i for i in r.json()["items"] if i["id"] == id_b)
        self.assertEqual(b_after["actual_start"], 260)

    def test_eval_matches_engine(self):
        a = self.client.post(f"{self.base}/items",
                             json=abs_body(target=3, duration=10)).json()
        id_a = a["items"][0]["id"]
        self.client.post(f"{self.base}/items",
                         json=abs_body(target=103, duration=100, ms=5))
        r = self.client.get(f"{self.base}/eval", params={"t": 4})
        ch = r.json()["channels"][0]
        self.assertEqual(ch["raw"], "6/5")
        self.assertEqual(ch["value"], 1)
        self.assertEqual(ch["active_item_id"], id_a)
        r2 = self.client.get(f"{self.base}/eval", params={"t": 5})
        ch2 = r2.json()["channels"][0]
        self.assertEqual(ch2["raw"], "3/2")
        self.assertEqual(ch2["value"], 2)

    def test_eval_decimal_time_end_to_end(self):
        self.client.post(f"{self.base}/items",
                        json=abs_body(target=3, duration=10))
        # t=1.5: 3 * 1.5/10 = 9/20, value 0; flooring to 1 would give 3/10
        r = self.client.get(f"{self.base}/eval?t=1.5")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["time_ms"], 1.5)
        ch = body["channels"][0]
        self.assertEqual(ch["raw"], "9/20")
        self.assertEqual(ch["value"], 0)
        # t=5 reaches the half-up point: 3/2 -> displayed 2
        r5 = self.client.get(f"{self.base}/eval?t=5").json()
        self.assertEqual(r5["time_ms"], 5)  # integral echo stays a JSON int
        self.assertEqual(r5["channels"][0]["raw"], "3/2")
        self.assertEqual(r5["channels"][0]["value"], 2)

    def test_eval_fraction_literal(self):
        self.client.post(f"{self.base}/items",
                        json=abs_body(target=3, duration=10))
        r = self.client.get(f"{self.base}/eval", params={"t": "5/3"})
        self.assertEqual(r.status_code, 200, r.text)
        ch = r.json()["channels"][0]
        self.assertEqual(ch["raw"], "1/2")
        self.assertEqual(ch["value"], 1)  # 0.5 rounds half up

    def test_eval_decimal_matches_integer_regression(self):
        self.client.post(f"{self.base}/items",
                         json=abs_body(target=100, duration=1000))
        for t, expected in ((0, 0), (250, 25), (500, 50),
                              (1000, 100), (2000, 100)):
            r = self.client.get(f"{self.base}/eval", params={"t": t})
            self.assertEqual(r.json()["time_ms"], t)
            self.assertEqual(r.json()["channels"][0]["value"], expected)

    def test_eval_rejects_bad_times(self):
        for bad in ("-1.5", "abc", "", "-0.1"):
            r = self.client.get(f"{self.base}/eval", params={"t": bad})
            self.assertEqual(r.status_code, 422, bad)
        # missing parameter is also a validation error
        self.assertEqual(self.client.get(f"{self.base}/eval").status_code, 422)

    def test_field_validation(self):
        r = self.client.post(f"{self.base}/items",
                             json=abs_body(target=256))
        self.assertEqual(r.status_code, 422)
        r = self.client.post(f"{self.base}/items",
                             json=abs_body(duration=-1))
        self.assertEqual(r.status_code, 422)
        r = self.client.post(f"{self.base}/items",
                             json={**abs_body(), "start_mode": "ref",
                                   "ref_id": 1, "ref_edge": "middle"})
        self.assertEqual(r.status_code, 422)

    def test_interrupted_flag(self):
        a = self.client.post(f"{self.base}/items",
                             json=abs_body(target=100, duration=1000)).json()
        id_a = a["items"][0]["id"]
        b = self.client.post(f"{self.base}/items",
                             json=abs_body(target=0, duration=100, ms=500)).json()
        id_b = b["items"][-1]["id"]
        items = {i["id"]: i for i in b["items"]}
        self.assertEqual(items[id_a]["interrupted_by"], id_b)
        self.assertIsNone(items[id_b]["interrupted_by"])

    def test_eval_high_precision_time_verbatim(self):
        # more than 6 fractional digits must reach the engine untouched —
        # no fixed 6-decimal rounding anywhere before evaluation
        self.client.post(f"{self.base}/items",
                         json=abs_body(target=100, duration=1000))
        r = self.client.get(f"{self.base}/eval", params={"t": "1.23456789"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["time_ms"], 1.23456789)
        ch = body["channels"][0]
        # 100 * t / 1000 with t = 123456789/100000000, computed exactly
        self.assertEqual(ch["raw"], "123456789/1000000000")
        self.assertEqual(ch["value"], 0)
        # the old 6-decimal rounding of the same moment would differ
        self.assertNotEqual(ch["raw"], str(Fraction("1.234568") / 10))

    def test_eval_high_precision_matches_direct_eval(self):
        # interface result == direct engine evaluation at the same instant,
        # including a probe just past an interruption boundary
        a = self.client.post(f"{self.base}/items",
                             json=abs_body(target=3, duration=10)).json()
        id_a = a["items"][0]["id"]
        b = self.client.post(f"{self.base}/items",
                             json=abs_body(target=103, duration=100, ms=5)).json()
        id_b = b["items"][-1]["id"]
        view = resolve([
            Item(id_a, "L1", 1, 3, 10, "abs", start_ms=0),
            Item(id_b, "L1", 1, 103, 100, "abs", start_ms=5),
        ])
        for text in ("1.23456789", "5.000000001", "7.5", "105"):
            t = Fraction(text)
            direct = evaluate(view, t)["L1|1"]
            r = self.client.get(f"{self.base}/eval", params={"t": text})
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["time_ms"], float(text) if t.denominator != 1
                             else t.numerator)
            ch = body["channels"][0]
            self.assertEqual(ch["raw"], direct["raw"], text)
            self.assertEqual(ch["value"], direct["value"], text)
            self.assertEqual(ch["active_item_id"], direct["active_item_id"], text)


    def test_long_reference_chain_saves_details_evals_and_rejects(self):
        # A legal chain well past Python's recursion limit (~500 levels) is
        # built directly in SQLite — this is the "库里已有同类工程" state in
        # which detail, eval and port used to blow up.  Build order mirrors
        # the UI repro: all absolute items first, then the earlier items are
        # changed one by one to reference the LATER item's end + 1ms, so
        # earlier ids depend on later ids.  Missing refs, cycles and
        # same-instant clashes stay rejected with the transaction rolled back.
        n = 520

        def ch(i):
            return ((i - 1) % 8) + 1

        pid = self.pid
        with db.transaction() as conn:
            order = db.next_sort_order(conn, pid)
            ids = []
            for i in range(1, n + 1):
                channel = 512 if i == 1 else ch(i)
                it = Item(None, "L1", channel, 100, 10, "abs", start_ms=i * 100)
                ids.append(db.insert_item(conn, pid, it, order))
                order += 1
            # rewire earlier ids onto later ids, one by one, no recursion
            for i in range(1, n):
                db.update_item(conn, pid, Item(
                    ids[i - 1], "L1", 512 if i == 1 else ch(i), 100, 10,
                    "ref", ref_id=ids[i], ref_edge="end", ref_offset=1))

        detail = self.client.get(self.base)
        self.assertEqual(detail.status_code, 200, detail.text)
        body = detail.json()
        self.assertEqual(len(body["items"]), n)
        by_id = {it["id"]: it for it in body["items"]}
        head, anchor = by_id[ids[0]], by_id[ids[-1]]
        self.assertEqual(head["start_mode"], "ref")
        self.assertEqual(head["ref_id"], ids[1])
        self.assertEqual(head["ref_edge"], "end")
        self.assertEqual(anchor["start_mode"], "abs")
        self.assertEqual(anchor["actual_start"], n * 100)
        self.assertEqual(head["actual_start"], n * 100 + (n - 1) * 11)
        # the projects list resolves every project without recursing either
        lst = self.client.get("/api/projects").json()
        row = next(p for p in lst if p["id"] == pid)
        self.assertEqual(row["item_count"], n)
        self.assertEqual(row["duration"], head["actual_start"] + 10)

        # fractional evaluation of the head item's fade (its own channel 512)
        mid = head["actual_start"] + 5.5
        ev = self.client.get(self.base + "/eval", params={"t": str(mid)})
        self.assertEqual(ev.status_code, 200, ev.text)
        lane = next(c for c in ev.json()["channels"] if c["channel"] == 512)
        self.assertEqual(lane["active_item_id"], ids[0])
        self.assertEqual(lane["raw"], "55")

        # closing the chain into a cycle is rejected AND rolled back
        r = self.client.put(
            f"{self.base}/items/{ids[-1]}",
            json=ref_body(ids[0], "start", offset=0, channel=ch(n),
                          duration=10))
        self.assertEqual(r.status_code, 400)
        self.assertIn("环", r.json()["detail"])
        still = self.client.get(self.base).json()["items"]
        self.assertEqual(next(i for i in still if i["id"] == ids[-1])
                         ["start_mode"], "abs")

        # a dangling reference is rejected AND rolled back
        r = self.client.put(
            f"{self.base}/items/{ids[0]}",
            json=ref_body(999999, "start", offset=0, channel=512,
                          duration=10))
        self.assertEqual(r.status_code, 400)
        self.assertIn("引用缺失", r.json()["detail"])
        after = {i["id"]: i for i in self.client.get(self.base).json()["items"]}
        self.assertEqual(after[ids[0]]["ref_id"], ids[1])

        # a same-instant collision on the head's lane is rejected AND
        # rolled back, leaving the project otherwise intact
        other = self.client.post(
            f"{self.base}/items",
            json=abs_body(fixture="L1", channel=512, ms=0, duration=10))
        self.assertEqual(other.status_code, 201, other.text)
        other_id = other.json()["items"][-1]["id"]
        r = self.client.put(
            f"{self.base}/items/{other_id}",
            json=abs_body(fixture="L1", channel=512,
                          ms=head["actual_start"], duration=10))
        self.assertEqual(r.status_code, 400)
        self.assertIn("同时起步", r.json()["detail"])
        kept = self.client.get(self.base).json()
        self.assertEqual(len(kept["items"]), n + 1)
        hit = next(i for i in kept["items"] if i["id"] == other_id)
        self.assertEqual(hit["actual_start"], 0)
        # the long chain itself is unchanged
        self.assertEqual(
            next(i for i in kept["items"] if i["id"] == ids[0])
            ["actual_start"], head["actual_start"])

    def test_long_chain_built_incrementally_over_http_saves(self):
        # the actual interactive repro, one HTTP write per item/edit:
        # absolute items first, then earlier items rewired onto later ones.
        # Kept shorter than the SQLite test so the suite stays fast, but it
        # is still far beyond the old recursion limit (~100 nested frames).
        n = 160
        ids = []
        for i in range(1, n + 1):
            r = self.client.post(
                f"{self.base}/items",
                json=abs_body(channel=((i - 1) % 8) + 1, duration=10,
                              ms=i * 100))
            self.assertEqual(r.status_code, 201, r.text)
            ids.append(r.json()["items"][-1]["id"])
        for i in range(1, n):
            r = self.client.put(
                f"{self.base}/items/{ids[i - 1]}",
                json=ref_body(ids[i], "end", offset=1,
                              channel=((i - 1) % 8) + 1, duration=10))
            self.assertEqual(r.status_code, 200, r.text)
        head = self.client.get(self.base).json()["items"][0]
        self.assertEqual(head["ref_edge"], "end")
        self.assertEqual(head["actual_start"], n * 100 + (n - 1) * 11)


if __name__ == "__main__":
    unittest.main()
