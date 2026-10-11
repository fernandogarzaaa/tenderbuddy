from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from tenderbuddy.ai import LLMError, MockLLM, OpenAICompatibleLLM, get_llm
from tenderbuddy.ai.features import (
    MAX_REQUIREMENTS,
    cell,
    clean_items,
    clip,
    draft_bid,
    extract_requirements,
    fmt_money,
    summarize_tender,
    template_bid_draft,
)
from tenderbuddy.ai.llm import extract_json, extractive_summary, sentences
from tenderbuddy.matcher import match_score
from tests.conftest import make_settings, mock_client

TEXT = ("The authority seeks a supplier for laptops. Laptops must have 16GB memory. "
        "Delivery is required within 30 days. The weather is nice. Laptops shall include a 3-year warranty.")


class FakeLLM:
    """Stand-in for a real model: returns a canned reply or raises."""

    name = "fake:model"

    def __init__(self, reply: str = "", error: bool = False) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[dict[str, object]] = []

    def complete(self, messages, *, json_mode=False, max_tokens=1200):  # type: ignore[no-untyped-def]
        self.calls.append({"messages": messages, "json_mode": json_mode, "max_tokens": max_tokens})
        if self.error:
            raise LLMError("boom")
        return self.reply


# ---------------------------------------------------------------- deterministic helpers

def test_sentences_and_summary() -> None:
    assert sentences("A b.  C d!\nE f?") == ["A b.", "C d!", "E f?"]
    summary = extractive_summary(TEXT, 2)
    assert summary.count(".") == 2
    assert "weather" not in summary
    assert summary == extractive_summary(TEXT, 2)  # deterministic
    assert extractive_summary("One. Two.", 3) == "One. Two."
    assert extractive_summary("", 3) == ""


def test_summary_keeps_original_order() -> None:
    s = extractive_summary(TEXT, 3)
    idx = [TEXT.index(part) for part in sentences(s)]
    assert idx == sorted(idx)


def test_extract_json_variants() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Sure!\n```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}
    assert extract_json('Here: {"a": 2} hope that helps') == {"a": 2}
    assert extract_json('list: ["x", "y"] end') == ["x", "y"]
    with pytest.raises(LLMError):
        extract_json("no json here")
    with pytest.raises(LLMError):
        extract_json("{broken")


def test_mock_llm() -> None:
    m = MockLLM()
    assert m.name == "mock"
    out = m.complete([{"role": "system", "content": "x"}, {"role": "user", "content": TEXT}])
    assert out == extractive_summary(TEXT, 3)
    assert json.loads(m.complete([{"role": "user", "content": TEXT}], json_mode=True)) == {"summary": out}
    assert m.complete([]) == ""


# ---------------------------------------------------------------- OpenAI-compatible client

def _ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_openai_client_request_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return _ok("  hello  ")

    llm = OpenAICompatibleLLM("https://llm.test/v1/", "sk-x", "m1", client=mock_client(handler))
    assert llm.name == "openai-compatible:m1"
    assert llm.complete([{"role": "user", "content": "hi"}], json_mode=True, max_tokens=50) == "hello"
    req = seen[0]
    assert str(req.url) == "https://llm.test/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer sk-x"
    body = json.loads(req.content)
    assert body == {"model": "m1", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.2,
                    "max_tokens": 50, "response_format": {"type": "json_object"}}


def test_openai_client_without_key_sends_no_auth() -> None:
    llm = OpenAICompatibleLLM("http://ollama.test/v1", "", "llama3", client=mock_client(lambda r: _ok("x")))
    assert "Authorization" not in llm.headers()
    assert llm.complete([{"role": "user", "content": "hi"}]) == "x"


def test_openai_client_drops_response_format_on_400() -> None:
    bodies: list[dict[str, object]] = []

    def handler(r: httpx.Request) -> httpx.Response:
        body = json.loads(r.content)
        bodies.append(body)
        return httpx.Response(400) if "response_format" in body else _ok("{}")

    llm = OpenAICompatibleLLM("https://l.test", "k", "m", client=mock_client(handler))
    assert llm.complete([{"role": "user", "content": "x"}], json_mode=True) == "{}"
    assert len(bodies) == 2
    assert "response_format" not in bodies[1]


def test_openai_client_retries_then_fails() -> None:
    sleeps: list[float] = []
    llm = OpenAICompatibleLLM("https://l.test", "k", "m", client=mock_client(lambda r: httpx.Response(503)),
                              max_retries=2, sleep=sleeps.append)
    with pytest.raises(LLMError, match="HTTP 503"):
        llm.complete([{"role": "user", "content": "x"}])
    assert sleeps == [1.0, 2.0]


def test_openai_client_retry_recovers() -> None:
    n = {"i": 0}

    def handler(r: httpx.Request) -> httpx.Response:
        n["i"] += 1
        return httpx.Response(429) if n["i"] == 1 else _ok("ok")

    llm = OpenAICompatibleLLM("https://l.test", "k", "m", client=mock_client(handler), sleep=lambda s: None)
    assert llm.complete([{"role": "user", "content": "x"}]) == "ok"


def test_openai_client_errors() -> None:
    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=r)

    llm = OpenAICompatibleLLM("https://l.test", "k", "m", client=mock_client(boom), max_retries=0)
    with pytest.raises(LLMError, match="ReadTimeout"):
        llm.complete([])
    for resp in (httpx.Response(401), httpx.Response(200, json={"nope": 1}), _ok("   "),
                 httpx.Response(200, json={"choices": [{"message": {"content": None}}]})):
        bad = OpenAICompatibleLLM("https://l.test", "k", "m", client=mock_client(lambda r, resp=resp: resp),
                                  max_retries=0)
        with pytest.raises(LLMError):
            bad.complete([])
    with pytest.raises(LLMError, match="LLM_BASE_URL"):
        OpenAICompatibleLLM("", "k", "m")


def test_get_llm_modes(tmp_path: Path) -> None:
    assert isinstance(get_llm(make_settings(tmp_path)), MockLLM)
    llm = get_llm(make_settings(tmp_path, LLM_PROVIDER="auto", LLM_BASE_URL="http://x/v1", LLM_MODEL="mm"))
    assert isinstance(llm, OpenAICompatibleLLM)
    assert llm.model == "mm"
    assert isinstance(get_llm(make_settings(tmp_path, LLM_PROVIDER="auto")), MockLLM)


# ---------------------------------------------------------------- features

def test_summarize_mock_and_llm_and_fallback() -> None:
    r = summarize_tender(MockLLM(), "Laptops", TEXT)
    assert (r.value, r.generator) == (extractive_summary(TEXT, 3), "rules")
    assert summarize_tender(MockLLM(), "Only a title", "  ").value == "Only a title"
    fake = FakeLLM("A crisp summary.")
    r2 = summarize_tender(fake, "Laptops", TEXT, max_chars=50)
    assert (r2.value, r2.generator) == ("A crisp summary.", "fake:model")
    user_msg = fake.calls[0]["messages"][1]["content"]  # type: ignore[index]
    assert user_msg.endswith("[truncated]")
    assert fake.calls[0]["max_tokens"] == 400
    r3 = summarize_tender(FakeLLM(error=True), "Laptops", TEXT)
    assert r3.value == extractive_summary(TEXT, 3)
    assert r3.generator == "rules (LLM fallback: boom)"


def test_extract_requirements_merges_and_dedupes() -> None:
    rules = ["Laptops must have 16GB memory.", "Delivery is required within 30 days."]
    assert extract_requirements(MockLLM(), TEXT, rules).value == rules
    assert extract_requirements(FakeLLM("{}"), "   ", rules).generator == "rules"
    fake = FakeLLM(json.dumps({"requirements": ["Bidder must hold ISO 9001 certificate",
                                                "laptops must have 16gb memory.", "short", 42,
                                                "Bidder must hold ISO 9001 certificate"]}))
    r = extract_requirements(fake, TEXT, rules)
    assert r.generator == "fake:model"
    assert r.value == ["Bidder must hold ISO 9001 certificate", "laptops must have 16gb memory.",
                       "Delivery is required within 30 days."]
    assert fake.calls[0]["json_mode"] is True
    as_list = extract_requirements(FakeLLM('["A requirement from a bare list"]'), TEXT, [])
    assert as_list.value == ["A requirement from a bare list"]
    bad = extract_requirements(FakeLLM("not json"), TEXT, rules)
    assert bad.value == rules
    assert bad.generator.startswith("rules (LLM fallback")
    many = extract_requirements(MockLLM(), TEXT, [f"Requirement number {i}" for i in range(60)])
    assert len(many.value) == MAX_REQUIREMENTS


def test_clean_items_and_helpers() -> None:
    assert clean_items("nope") == []
    assert clean_items(["  spaced    out item here ", "x" * 501]) == ["spaced out item here"]
    assert clip("abc", 5) == "abc"
    assert clip("abcdef", 3) == "abc\n[truncated]"
    assert cell("a | b\n c") == "a \\| b c"
    assert fmt_money(None, "EUR") == "not stated"
    assert fmt_money(0, "EUR") == "not stated"
    assert fmt_money(1500.4, "EUR") == "EUR 1,500"


PROFILE = {"name": "Acme Ltd", "currency": "EUR", "annual_turnover": 2_000_000, "years_experience": 6,
           "certifications": ["ISO 9001"], "categories": ["IT Services"], "regions": ["IRL"],
           "cpv_prefixes": [], "keywords": [], "capability_statement": ""}
TENDER = {"title": "Laptop supply", "ref_no": "T-1", "authority": "Dept of Education",
          "deadline": datetime(2026, 11, 30, 12, 0), "description": "Supply of 500 laptops.", "summary": ""}


def test_template_draft_contents() -> None:
    match = match_score(PROFILE, {"currency": "EUR", "min_turnover": 5_000_000, "certs_required": ["ISO 9001"],
                                  "country": "IRL"})
    reqs = [{"text": "Provide 3 references | signed", "is_met": False}, {"text": "ISO cert", "is_met": True}]
    md = template_bid_draft(PROFILE, TENDER, match, reqs, datetime(2026, 10, 11))
    assert md.startswith("# Bid response: Laptop supply\n")
    assert "**Submission deadline:** 30 November 2026 12:00 UTC" in md
    assert "**Prepared by:** Acme Ltd on 11 October 2026" in md
    assert "Dear Dept of Education," in md
    assert "Acme Ltd has 6 years of relevant experience in IT Services." in md
    assert "- Annual turnover: EUR 2,000,000" in md
    assert f"Self-assessed eligibility score: **{match.score}/100**." in md
    assert "| Annual turnover | >= EUR 5,000,000 | EUR 2,000,000 | partial |" in md
    assert "| 1 | Provide 3 references \\| signed | To confirm | |" in md
    assert "| 2 | ISO cert | Complied | |" in md
    assert "Supply of 500 laptops." in md
    assert "- Annual turnover: required >= EUR 5,000,000; ours EUR 2,000,000." in md
    assert "- Confirm: Provide 3 references | signed" in md
    assert "- Confirm: ISO cert" not in md
    assert md.endswith("\n")


def test_template_draft_empty_cases() -> None:
    p = dict(PROFILE, name="", categories=[], capability_statement="We build things.", certifications=[],
             regions=[])
    t = dict(TENDER, authority="", ref_no="", description="", summary="")
    md = template_bid_draft(p, t, match_score({}, {}), [], datetime(2026, 1, 1))
    assert "[Company name]" in md
    assert "Dear Sir/Madam," in md
    assert "We build things." in md
    assert "- Certifications: none listed" in md
    assert "- Operating regions: not specified" in md
    assert "No requirements captured yet." in md
    assert "See the published notice." in md
    assert "- None identified. Final review and sign-off." in md
    assert "(ref. -)" in md


def test_draft_bid_generators() -> None:
    match = match_score(PROFILE, {})
    today = datetime(2026, 10, 11)
    base = template_bid_draft(PROFILE, TENDER, match, [], today)
    r = draft_bid(MockLLM(), PROFILE, TENDER, match, [], today)
    assert (r.value, r.generator) == (base, "template")
    fake = FakeLLM("# Polished")
    r2 = draft_bid(fake, PROFILE, TENDER, match, [], today)
    assert (r2.value, r2.generator) == ("# Polished", "fake:model")
    assert "Supply of 500 laptops." in fake.calls[0]["messages"][1]["content"]  # type: ignore[index]
    assert fake.calls[0]["max_tokens"] == 3000
    r3 = draft_bid(FakeLLM(error=True), PROFILE, TENDER, match, [], today)
    assert r3.value == base
    assert r3.generator == "template (LLM fallback: boom)"
