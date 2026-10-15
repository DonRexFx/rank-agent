"""Phase 1 tests (matching, cache, Gemini quota errors, partial results, request budget).
Only the HTTP call to Google is faked; the real code paths run. No API key or network needed.
Run: python -m unittest -v test_phase1"""
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ["APP_PASSWORD"] = "secret123"
os.environ["GEMINI_API_KEY"] = "AIzaSyFAKEKEY1234567890abcdef"
os.environ["CACHE_TTL_SECONDS"] = "0"  # the app-level default cache is off in tests

import agent_core  # noqa: E402
import app as webapp  # noqa: E402
import budget  # noqa: E402
import cache as cache_mod  # noqa: E402
import gemini  # noqa: E402
import tracker  # noqa: E402
from gemini import GeminiError  # noqa: E402

REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"


# ---------- helpers ----------
class HTTP:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def ok(text="An answer.", host_titles=("other.com",), grounded=True):
    cand = {"content": {"parts": [{"text": text}]}}
    if grounded:
        cand["groundingMetadata"] = {"webSearchQueries": ["q"],
                                     "groundingChunks": [{"web": {"uri": REDIRECT + str(i), "title": t}} for i, t in enumerate(host_titles)]}
    return HTTP(200, {"candidates": [cand]})


def err(status, message, quota_id=None, retry_delay=None, headers=None, gstatus="RESOURCE_EXHAUSTED"):
    details = []
    if quota_id:
        details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests", "quotaId": quota_id}]})
    if retry_delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay})
    return HTTP(status, {"error": {"code": status, "message": message, "status": gstatus, "details": details}}, headers)


class Harness:
    """Patches requests.post, time.sleep and the call budget around gemini.generate()."""

    def __init__(self, responses, daily=1000, run=100):
        self.responses, self.posts, self.sleeps = list(responses), 0, []
        self.budget = budget.CallBudget(daily, run)

    def post(self, *a, **k):
        self.posts += 1
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    def __enter__(self):
        self.patches = [mock.patch.object(gemini.requests, "post", self.post),
                        mock.patch.object(gemini.time, "sleep", lambda s: self.sleeps.append(s)),
                        mock.patch.object(budget, "BUDGET", self.budget)]
        for p in self.patches:
            p.start()
        self.budget.begin_run()
        return self

    def __exit__(self, *exc):
        self.budget.end_run()
        for p in self.patches:
            p.stop()


# ---------- A. matching ----------
class TestMatching(unittest.TestCase):
    def test_whole_word_only(self):
        self.assertFalse(tracker.find_brand("Try Acmeron Dental for fillings.", "Acme")[0])
        self.assertFalse(tracker.find_brand("Pacme is another brand.", "Acme")[0])
        self.assertFalse(tracker.find_brand("Sweetness bakery is nice.", "Sweet")[0])

    def test_possessive_punctuation_accents_spacing(self):
        self.assertTrue(tracker.find_brand("Acme's team is great.", "Acme")[0])
        self.assertTrue(tracker.find_brand("We recommend Becky’s Pastries in Yaba.", "Becky's Pastries")[0])
        self.assertTrue(tracker.find_brand("Try Beckys Pastries.", "Becky's Pastries")[0])
        self.assertTrue(tracker.find_brand("Acme    Dental, the clinic.", "acme dental")[0])
        self.assertTrue(tracker.find_brand("Visit Café Noir today.", "Cafe Noir")[0])

    def test_empty_brand_never_matches(self):
        self.assertFalse(tracker.find_brand("anything", "")[0])
        self.assertFalse(tracker.find_brand("anything", "!!!")[0])

    def test_negative_statement_is_not_a_mention(self):
        said, snippet, neg = tracker.find_brand("I could not find any information about Acme Dental.", "Acme Dental")
        self.assertFalse(said)
        self.assertTrue(neg)
        self.assertIn("Acme Dental", snippet)

    def test_mixed_sentences_use_the_positive_one(self):
        said, snippet, neg = tracker.find_brand("I couldn't find Acme Dental's pricing. Acme Dental is a clinic in Yaba.", "Acme Dental")
        self.assertTrue(said)
        self.assertIn("clinic in Yaba", snippet)
        self.assertFalse(neg)


class TestDomains(unittest.TestCase):
    def test_normalize(self):
        for raw in ("https://www.Acme.com/path?x=1", "  ACME.COM  ", "acme.com:8080", "http://acme.com#top", "user@acme.com"):
            self.assertEqual(tracker.normalize_domain(raw), "acme.com", raw)
        self.assertEqual(tracker.normalize_domain(""), "")
        self.assertEqual(tracker.normalize_domain("blog.acme.co.uk"), "blog.acme.co.uk")

    def test_invalid_rejected(self):
        for bad in ("localhost", "acme", "1.2.3.4", "not a domain", "acme..com", "-acme.com", "http://", "javascript:alert(1)", "acme.c"):
            with self.assertRaises(ValueError, msg=bad):
                tracker.normalize_domain(bad)

    def test_host_matching_is_exact_or_subdomain(self):
        self.assertTrue(tracker.host_matches("acme.com", "acme.com"))
        self.assertTrue(tracker.host_matches("blog.acme.com", "acme.com"))
        self.assertFalse(tracker.host_matches("notacme.com", "acme.com"))
        self.assertFalse(tracker.host_matches("acme.com.evil.net", "acme.com"))
        self.assertFalse(tracker.host_matches(None, "acme.com"))
        self.assertFalse(tracker.host_matches("acme.com", ""))

    def test_source_host_extraction(self):
        self.assertEqual(tracker.source_host({"uri": REDIRECT + "1", "title": "acme.com"}), "acme.com")
        self.assertIsNone(tracker.source_host({"uri": REDIRECT + "1", "title": "Acme Dental - Home"}))
        self.assertEqual(tracker.source_host({"uri": "https://www.example.org/p", "title": "x"}), "example.org")


# ---------- C. accurate results ----------
def raw(text="Some answer.", titles=("other.com",), grounded=True):
    return {"text": text, "grounded": grounded, "model": "m",
            "sources": [{"title": t, "uri": REDIRECT + str(i)} for i, t in enumerate(titles)]}


class TestStatuses(unittest.TestCase):
    def test_cited(self):
        r = tracker.analyze("Best dentist?", "Acme Dental", "acme.com", raw(titles=("acme.com", "other.com")))
        self.assertEqual(r["status"], "cited")
        self.assertIn("acme.com", r["evidence"])

    def test_cited_requires_real_domain_not_substring(self):
        r = tracker.analyze("Best dentist?", "Acme Dental", "acme.com", raw(titles=("notacme.com",)))
        self.assertEqual(r["status"], "not_found")
        self.assertIs(r["cited"], False)

    def test_mentioned(self):
        r = tracker.analyze("Best dentist?", "Acme Dental", "acme.com", raw("Acme Dental is a well-reviewed clinic."))
        self.assertEqual(r["status"], "mentioned")
        self.assertIn("well-reviewed", r["evidence"])

    def test_not_found_is_only_claimed_with_live_search(self):
        r = tracker.analyze("Best dentist?", "Acme Dental", "acme.com", raw("Try Other Clinic."))
        self.assertEqual(r["status"], "not_found")
        self.assertIs(r["mentioned"], False)

    def test_ungrounded_absence_is_unverified_not_negative(self):
        r = tracker.analyze("Best dentist?", "Acme Dental", "acme.com", raw("Try Other Clinic.", titles=(), grounded=False))
        self.assertEqual(r["status"], "unverified")
        self.assertIsNone(r["mentioned"])
        self.assertIsNone(r["cited"])
        self.assertIn("live web search", r["reason"])

    def test_ungrounded_but_named_is_still_a_mention(self):
        r = tracker.analyze("Best dentist?", "Acme Dental", "", raw("Acme Dental is popular.", titles=(), grounded=False))
        self.assertEqual(r["status"], "mentioned")

    def test_question_naming_the_brand_is_not_evidence(self):
        r = tracker.analyze("Is Acme Dental any good?", "Acme Dental", "", raw("Acme Dental is a clinic."))
        self.assertEqual(r["status"], "unverified")
        self.assertIn("names the brand", r["reason"])
        r2 = tracker.analyze("Is Acme Dental any good?", "Acme Dental", "acme.com", raw("x", titles=("acme.com",)))
        self.assertEqual(r2["status"], "cited")  # a real source is still real evidence

    def test_unreadable_source_domain_blocks_a_negative(self):
        r = tracker.analyze("Best dentist?", "Acme Dental", "acme.com", raw("Try Other.", titles=("Acme Dental - Home",)))
        self.assertEqual(r["status"], "unverified")
        self.assertIn("no readable domain", r["reason"])

    def test_no_domain_given(self):
        r = tracker.analyze("Best dentist?", "Acme Dental", "", raw("Try Other."))
        self.assertEqual(r["status"], "not_found")
        self.assertIsNone(r["cited"])

    def test_negative_only_mention_is_not_a_mention(self):
        r = tracker.analyze("Best dentist?", "Acme Dental", "", raw("I could not find information on Acme Dental."))
        self.assertEqual(r["status"], "not_found")
        self.assertIn("negative", r["evidence"])

    def test_each_source_keeps_its_own_url(self):
        # An unreadable first source must not shift the others (the old zip() bug).
        r = tracker.analyze("q", "Acme", "acme.com", {"text": "x", "grounded": True, "sources": [
            {"title": "", "uri": REDIRECT + "x1"}, {"title": "acme.com", "uri": REDIRECT + "x2"}]})
        self.assertIsNone(r["sources"][0]["host"])
        self.assertEqual((r["sources"][1]["host"], r["sources"][1]["uri"]), ("acme.com", REDIRECT + "x2"))
        self.assertEqual(r["status"], "cited")

    def test_sources_do_not_leak_between_questions(self):
        answers = {"First question?": raw("a", ("one.com",)), "Second question?": raw("b", ("two.com",))}
        with mock.patch.object(tracker, "generate", lambda q, **k: dict(answers[q], search_queries=["s"])):
            results, _ = tracker.track(list(answers), "Acme", "acme.com", pause=0, cache=cache_mod.TTLCache(":memory:", 0))
        self.assertEqual(results[0]["cited_hosts"], ["one.com"])
        self.assertEqual(results[1]["cited_hosts"], ["two.com"])

    def test_report_excludes_unverified_from_rates(self):
        rs = [tracker.analyze("q1", "Acme", "acme.com", raw("x", ("acme.com",))),
              tracker.analyze("q2", "Acme", "acme.com", raw("x", ("other.com",))),
              tracker.analyze("q3", "Acme", "acme.com", raw("Acme is great.", ("other.com",))),
              tracker.unverified_result("q4", "boom")]
        md = tracker.tracking_markdown("Acme", "acme.com", rs)
        self.assertIn("2 of 3 verified", md)          # mentioned: cited + mentioned out of 3 verified
        self.assertIn("1 of 3 verified", md)          # cited
        self.assertIn("Couldn't verify:** 1 of 4", md)
        self.assertIn("| q4 |", md)
        self.assertIn("boom", md)
        self.assertIn("Questions that could not be verified", md)
        self.assertIn("| Customer question | Mentioned | Cited | Result |", md)


# ---------- B. cache ----------
class TestCache(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.c = cache_mod.TTLCache(":memory:", ttl=100, clock=lambda: self.t[0])

    def test_set_get_and_expiry(self):
        k = cache_mod.make_key(a=1)
        self.assertTrue(self.c.set(k, {"x": 1}))
        self.assertEqual(self.c.get(k), {"x": 1})
        self.t[0] += 99
        self.assertEqual(self.c.get(k), {"x": 1})
        self.t[0] += 2
        self.assertIsNone(self.c.get(k))          # expired is never returned
        self.assertIsNone(self.c.get_entry(k))

    def test_ttl_zero_disables(self):
        c = cache_mod.TTLCache(":memory:", ttl=0)
        self.assertFalse(c.set("k", {"x": 1}))
        self.assertIsNone(c.get("k"))

    def test_key_includes_all_parts(self):
        base = tracker._cache_key("Best cake?", "Acme", "acme.com")
        self.assertEqual(base, tracker._cache_key("  best   CAKE?? ", "ACME", "acme.com"))
        self.assertNotEqual(base, tracker._cache_key("Other question?", "Acme", "acme.com"))
        self.assertNotEqual(base, tracker._cache_key("Best cake?", "Other", "acme.com"))
        self.assertNotEqual(base, tracker._cache_key("Best cake?", "Acme", "other.com"))
        with mock.patch.object(gemini, "MODEL", "another-model"):
            self.assertNotEqual(base, tracker._cache_key("Best cake?", "Acme", "acme.com"))

    def test_failures_are_not_cached(self):
        calls = []

        def failing(q, **k):
            calls.append(q)
            raise GeminiError("down", kind="server")
        with mock.patch.object(tracker, "generate", failing):
            with self.assertRaises(GeminiError):
                tracker._ask("Q?", "Acme", "acme.com", self.c)
        self.assertIsNone(self.c.get_entry(tracker._cache_key("Q?", "Acme", "acme.com")))
        good = lambda q, **k: dict(raw("fine"), search_queries=["s"])
        with mock.patch.object(tracker, "generate", good):
            _, from_cache, _ = tracker._ask("Q?", "Acme", "acme.com", self.c)
        self.assertFalse(from_cache)                # the earlier failure did not poison the cache

    def test_ungrounded_answers_are_not_cached(self):
        with mock.patch.object(tracker, "generate", lambda q, **k: dict(raw("x", (), grounded=False), search_queries=[])):
            tracker._ask("Q?", "Acme", "acme.com", self.c)
        self.assertIsNone(self.c.get_entry(tracker._cache_key("Q?", "Acme", "acme.com")))

    def test_cache_hit_makes_no_request_and_uses_no_budget(self):
        with Harness([ok("Acme Dental is great.", ("acme.com",))]) as h:
            r1, _ = tracker.track(["Best dentist?"], "Acme Dental", "acme.com", pause=0, cache=self.c)
            r2, info = tracker.track(["Best dentist?"], "Acme Dental", "acme.com", pause=0, cache=self.c)
            self.assertEqual(h.posts, 1)
            self.assertEqual(h.budget._used, 1)
        self.assertFalse(r1[0]["cached"])
        self.assertTrue(r2[0]["cached"])
        self.assertEqual(info["cached"], 1)
        self.assertEqual(r2[0]["status"], r1[0]["status"])
        self.assertIn("reused from a saved result", tracker.tracking_markdown("Acme Dental", "acme.com", r2))

    def test_expired_entry_triggers_a_fresh_request(self):
        with Harness([ok("x", ("other.com",))]) as h:
            tracker.track(["Q one?"], "Acme", "acme.com", pause=0, cache=self.c)
            self.t[0] += 101
            tracker.track(["Q one?"], "Acme", "acme.com", pause=0, cache=self.c)
            self.assertEqual(h.posts, 2)

    def test_file_persistence_and_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.sqlite3")
            a = cache_mod.TTLCache(path, ttl=100, clock=lambda: 5.0)
            a.set("k", {"v": 1})
            b = cache_mod.TTLCache(path, ttl=100, clock=lambda: 50.0)
            self.assertTrue(b.persistent)
            self.assertEqual(b.get("k"), {"v": 1})
            late = cache_mod.TTLCache(path, ttl=100, clock=lambda: 500.0)
            self.assertIsNone(late.get("k"))        # expired across a restart too
        bad = cache_mod.TTLCache("/nonexistent-dir-xyz/c.sqlite3", ttl=100)
        self.assertFalse(bad.persistent)
        bad.set("k", 1)
        self.assertEqual(bad.get("k"), 1)


# ---------- A. Gemini errors ----------
DAILY = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
MINUTE = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"


class TestGeminiErrors(unittest.TestCase):
    def call(self, responses, **kw):
        with Harness(responses) as h:
            try:
                return h, gemini.generate("q", search=True, **kw), None
            except GeminiError as e:
                return h, None, e

    def test_daily_quota_not_retried_and_message_preserved(self):
        h, _, e = self.call([err(429, "Quota exceeded for metric: free_tier_requests, limit: 20", DAILY, "34s")])
        self.assertEqual((e.kind, e.status, e.google_status), ("quota_exhausted", 429, "RESOURCE_EXHAUSTED"))
        self.assertEqual((h.posts, h.sleeps), (1, []))
        self.assertIn("Quota exceeded for metric", str(e))
        self.assertIn("429", str(e))
        self.assertTrue(e.abort_run)

    def test_limit_zero_means_no_free_allowance(self):
        h, _, e = self.call([err(429, "You exceeded your current quota ... limit: 0, model: x")])
        self.assertEqual(e.kind, "quota_exhausted")
        self.assertEqual(h.posts, 1)

    def test_minute_limit_honours_retry_after_header(self):
        h, res, e = self.call([err(429, "slow down", MINUTE, "20s", {"Retry-After": "3"}), ok("fine")])
        self.assertIsNone(e)
        self.assertEqual(res["text"], "fine")
        self.assertEqual((h.posts, h.sleeps), (2, [3.0]))   # header wins over RetryInfo

    def test_minute_limit_uses_retryinfo(self):
        h, res, _ = self.call([err(429, "slow down", MINUTE, "12s"), ok("fine")])
        self.assertEqual((h.posts, h.sleeps, res["text"]), (2, [12.0], "fine"))

    def test_long_wait_is_not_retried(self):
        h, _, e = self.call([err(429, "slow down", MINUTE, "120s")])
        self.assertEqual((e.kind, e.retry_after, h.posts, h.sleeps), ("rate_limit", 120.0, 1, []))
        self.assertIn("120", str(e))

    def test_persistent_temporary_limit_stops_after_configured_retries(self):
        h, _, e = self.call([err(429, "slow down", MINUTE)])
        self.assertEqual(e.kind, "rate_limit")
        self.assertEqual(h.posts, gemini.MAX_RETRIES + 1)
        self.assertEqual(len(h.sleeps), gemini.MAX_RETRIES)

    def test_wrong_model_not_retried(self):
        h, _, e = self.call([err(404, "models/old is no longer available to new users", gstatus="NOT_FOUND")])
        self.assertEqual((e.kind, h.posts), ("model_unavailable", 1))
        self.assertIn("no longer available", str(e))
        self.assertIn("GEMINI_MODEL", str(e))

    def test_bad_key_not_retried(self):
        h, _, e = self.call([err(400, "API key not valid. Please pass a valid API key.", gstatus="INVALID_ARGUMENT")])
        self.assertEqual((e.kind, h.posts), ("auth", 1))

    def test_server_error_is_retried_then_succeeds(self):
        h, res, _ = self.call([HTTP(503, {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}}), ok("fine")])
        self.assertEqual((h.posts, res["text"]), (2, "fine"))

    def test_network_error_retried(self):
        h, res, _ = self.call([gemini.requests.ConnectionError("boom"), ok("fine")])
        self.assertEqual((h.posts, res["text"]), (2, "fine"))
        h, _, e = self.call([gemini.requests.ConnectionError("boom")])
        self.assertEqual((e.kind, h.posts), ("network", gemini.MAX_RETRIES + 1))

    def test_secrets_never_appear_in_errors(self):
        key = os.environ["GEMINI_API_KEY"]
        h, _, e = self.call([err(404, f"bad request to https://x/v1?key={key} and Bearer abc.def.ghi and AIzaSyOTHERKEY0123456789", gstatus="NOT_FOUND")])
        text = str(e)
        for secret in (key, "AIzaSyOTHERKEY0123456789", "abc.def.ghi"):
            self.assertNotIn(secret, text)
        self.assertIn("[hidden]", text)

    def test_grounded_flag(self):
        _, res, _ = self.call([ok("x", ("a.com",))])
        self.assertTrue(res["grounded"])
        _, res, _ = self.call([ok("x", grounded=False)])
        self.assertFalse(res["grounded"])

    def test_empty_or_blocked_answers_are_errors(self):
        _, _, e = self.call([HTTP(200, {"candidates": [{"content": {"parts": [{"text": ""}]}, "finishReason": "SAFETY"}]})])
        self.assertEqual(e.kind, "blocked")
        _, _, e = self.call([HTTP(200, {"candidates": []})])
        self.assertIn(e.kind, ("blocked", "empty"))


# ---------- E. budget ----------
class TestBudget(unittest.TestCase):
    def test_concurrent_reservations_never_exceed_the_limit(self):
        b, granted = budget.CallBudget(50, 0), []

        def worker():
            try:
                b.reserve()
                granted.append(1)
            except budget.BudgetExceeded:
                pass
        threads = [threading.Thread(target=worker) for _ in range(200)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(granted), 50)
        self.assertEqual(b.remaining(), 0)

    def test_per_run_limit_and_isolation(self):
        b = budget.CallBudget(100, 3)
        b.begin_run()
        for _ in range(3):
            b.reserve()
        with self.assertRaises(budget.BudgetExceeded) as cm:
            b.reserve()
        self.assertEqual(cm.exception.scope, "run")
        b.end_run()
        b.begin_run()                               # a new run gets a fresh allowance
        b.reserve()
        self.assertEqual(b.remaining(), 100 - 4)    # the refused call was not counted

    def test_daily_limit_resets_at_utc_midnight(self):
        t = [0.0]
        b = budget.CallBudget(2, 0, clock=lambda: t[0])
        b.reserve()
        b.reserve()
        with self.assertRaises(budget.BudgetExceeded):
            b.reserve()
        t[0] += 86400
        b.reserve()
        self.assertEqual(b.remaining(), 1)

    def test_zero_means_unlimited(self):
        b = budget.CallBudget(0, 0)
        for _ in range(500):
            b.reserve()
        self.assertIsNone(b.remaining())

    def test_retries_cannot_exceed_the_run_budget(self):
        with Harness([HTTP(503, {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})], run=2) as h:
            with self.assertRaises(GeminiError) as cm:
                gemini.generate("q", retries=5)
            self.assertEqual(cm.exception.kind, "budget")
            self.assertEqual(h.posts, 2)            # attempts 3..6 were never sent

    def test_budget_exhaustion_stops_a_track_run_cleanly(self):
        with Harness([ok("x", ("other.com",))], run=2) as h:
            results, info = tracker.track(["Q1 a?", "Q2 b?", "Q3 c?", "Q4 d?"], "Acme", "acme.com", pause=0,
                                          cache=cache_mod.TTLCache(":memory:", 0))
            self.assertEqual(h.posts, 2)
        self.assertEqual([r["status"] for r in results], ["not_found", "not_found", "unverified", "unverified"])
        self.assertEqual(info["first_error"].kind, "budget")


# ---------- D. partial results ----------
def ans(q, **k):
    return dict(raw(f"Answer to {q}", ("other.com",)), search_queries=["s"])


class TestPartialResults(unittest.TestCase):
    def run_track(self, behaviour, questions=("Q1 aaa?", "Q2 bbb?", "Q3 ccc?")):
        calls = []

        def fake(q, **k):
            calls.append(q)
            return behaviour(q)
        with mock.patch.object(tracker, "generate", fake):
            results, info = tracker.track(list(questions), "Acme", "acme.com", pause=0, cache=cache_mod.TTLCache(":memory:", 0))
        return results, info, calls

    def test_one_failure_keeps_the_other_results(self):
        def behaviour(q):
            if q.startswith("Q2"):
                raise GeminiError("Gemini error 503 (UNAVAILABLE): overloaded", 503, "server")
            return ans(q)
        results, info, calls = self.run_track(behaviour)
        self.assertEqual([r["status"] for r in results], ["not_found", "unverified", "not_found"])
        self.assertEqual(len(calls), 3)             # a non-fatal failure does not stop later questions
        md = tracker.tracking_markdown("Acme", "acme.com", results)
        self.assertIn("overloaded", md)
        self.assertIn("0 of 2 verified", md)            # rates use the 2 verified answers only
        self.assertIn("| Q2 bbb? | Couldn't verify | Couldn't verify | Couldn't verify |", md)  # not shown as "No"

    def test_fatal_error_stops_further_requests_and_marks_rest_unverified(self):
        def behaviour(q):
            if q.startswith("Q2"):
                raise GeminiError("Gemini error 429: Quota exceeded", 429, "quota_exhausted")
            return ans(q)
        results, info, calls = self.run_track(behaviour)
        self.assertEqual(calls, ["Q1 aaa?", "Q2 bbb?"])   # Q3 was never sent
        self.assertEqual([r["status"] for r in results], ["not_found", "unverified", "unverified"])
        self.assertTrue(results[2]["reason"].startswith("not sent"))
        self.assertEqual(info["aborted_at"], 2)

    def test_run_track_returns_partial_report(self):
        def behaviour(q):
            if q.startswith("Q2"):
                raise GeminiError("overloaded", 503, "server")
            return ans(q)
        with mock.patch.object(tracker, "generate", lambda q, **k: behaviour(q)), \
                mock.patch.object(cache_mod, "default_cache", lambda: cache_mod.TTLCache(":memory:", 0)), \
                mock.patch.object(tracker.time, "sleep", lambda s: None):
            out = agent_core.run_track("Acme", "acme.com", ["Q1 aaa?", "Q2 bbb?", "Q3 ccc?"], "Mama Tee")
        self.assertTrue(out["partial"])
        self.assertEqual(out["warnings"], ["1 of 3 questions could not be verified."])
        self.assertIn("Couldn't verify", out["report"])
        self.assertIn("Mama Tee", out["report"])

    def test_nothing_verified_raises_the_real_error(self):
        boom = GeminiError("Gemini error 429: daily quota", 429, "quota_exhausted")

        def always(q, **k):
            raise boom
        with mock.patch.object(tracker, "generate", always), \
                mock.patch.object(cache_mod, "default_cache", lambda: cache_mod.TTLCache(":memory:", 0)):
            with self.assertRaises(GeminiError) as cm:
                agent_core.run_track("Acme", "acme.com", ["Q1 aaa?", "Q2 bbb?"])
        self.assertIn("daily quota", str(cm.exception))

    def test_duplicate_questions_are_sent_once(self):
        results, _, calls = self.run_track(lambda q: ans(q), ("Same question?", "same   question", "Other one?"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(results), 2)


# ---------- app wiring ----------
class TestApp(unittest.TestCase):
    H = {"X-Access-Code": "secret123"}

    def setUp(self):
        self.c = webapp.app.test_client()
        webapp.RECENT.clear()
        webapp.JOBS.clear()
        # No test may reach the real network: every started job uses a quick fake unless a test overrides it.
        p = mock.patch.object(webapp.agent_core, "run_track", lambda *a, **k: {"report": "# r", "scores": None})
        p.start()
        self.addCleanup(p.stop)

    def wait(self, jid):
        for _ in range(100):
            j = self.c.get(f"/api/status/{jid}", headers=self.H).get_json()
            if j["status"] != "running":
                return j
            time.sleep(0.05)
        self.fail("job timed out")

    def start(self, **body):
        return self.c.post("/api/start", json=dict({"mode": "track", "brand": "Acme Dental", "domain": "acme.com",
                                                    "prompts": "Best dentist in Lagos?"}, **body), headers=self.H)

    def test_invalid_domain_and_short_brand_rejected(self):
        self.assertEqual(self.start(domain="not a domain").status_code, 400)
        self.assertEqual(self.start(brand="Ab").status_code, 400)
        self.assertEqual(self.start(domain="https://www.acme.com/x").status_code, 200)  # normalised, accepted

    def test_blank_domain_still_allowed(self):
        self.assertEqual(self.start(domain="").status_code, 200)

    def test_exhausted_daily_budget_refuses_before_starting_a_job(self):
        b = budget.CallBudget(1, 15)
        b.reserve()
        with mock.patch.object(budget, "BUDGET", b):
            r = self.start()
        self.assertEqual(r.status_code, 429)
        self.assertIn("daily AI call budget", r.get_json()["error"])

    def test_partial_flag_and_warnings_reach_the_status_response(self):
        fake = lambda *a, **k: {"report": "# r", "scores": None, "partial": True, "warnings": ["1 of 2 questions could not be verified."]}
        with mock.patch.object(webapp.agent_core, "run_track", fake):
            j = self.wait(self.start().get_json()["id"])
        self.assertEqual((j["status"], j["partial"]), ("done", True))
        self.assertEqual(j["warnings"], ["1 of 2 questions could not be verified."])

    def test_gemini_error_text_is_shown_without_secrets(self):
        def boom(*a, **k):
            raise GeminiError(gemini._clean("Gemini error 429 (RESOURCE_EXHAUSTED): Quota exceeded " + os.environ["GEMINI_API_KEY"]), 429, "quota_exhausted")
        with mock.patch.object(webapp.agent_core, "run_track", boom):
            j = self.wait(self.start().get_json()["id"])
        self.assertEqual(j["status"], "error")
        self.assertIn("Quota exceeded", j["error"])
        self.assertNotIn(os.environ["GEMINI_API_KEY"], j["error"])

    def test_worker_cleans_up_the_run_budget(self):
        with mock.patch.object(webapp.agent_core, "run_track", lambda *a, **k: {"report": "# r", "scores": None}):
            self.wait(self.start().get_json()["id"])
        self.assertIsNone(getattr(budget.BUDGET._local, "run", None))  # main thread untouched


if __name__ == "__main__":
    unittest.main(verbosity=2)
