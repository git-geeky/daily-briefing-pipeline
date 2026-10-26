"""Subprocess harness: run the real conductor against a scripted fake model (no network, no real LLM).

Usage: python e2e_harness.py <scenario> <root> [--second-pass]
Prints one JSON line: {"exit": int, "calls": {...}, "files": {...}, "state": {...}} for the test to assert on.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SCRIPTS))

scenario, root = sys.argv[1], Path(sys.argv[2])
second_pass = "--second-pass" in sys.argv
os.environ.update({"BRIEFING_EDITION": "animal", "DAILY_BRIEFING_ROOT": str(root), "DAILY_BRIEFING_SHARED_ROOT": str(REPO)})
DATE = "2026-10-26"

import animal_fixtures as fx  # noqa: E402
import conductor_core as cc  # noqa: E402
import edition_core  # noqa: E402
import llm_backend_core as llm  # noqa: E402
import slate_core  # noqa: E402
from test_dossier import FakeWeb, html_bytes  # noqa: E402
from test_photo_core import FakeFetcher, commons_page  # noqa: E402

edition = edition_core.load_edition("animal")
catalog = fx.catalog_urls(4)
WORDS = int(os.environ.get("E2E_WORDS", "1700"))

audit_fixture = root / "audit-fixture.json"
import source_audit_core as audit  # noqa: E402

entries = audit.parse_source_catalogs()
animal = [e for e in entries if e.catalog_number >= 401][:6]
audit_fixture.write_text(json.dumps({
    "default_status": "checked_no_relevant_update",
    "sources": [{"source_id": e.source_id, "items": [
        {"title": f"Telemetry study of survival in a monitored animal population reported by {e.name[:30]}", "url": f"https://example.org/{i}-{k}", "published": "2026-10-25"}
        for k in range(2)]} for i, e in enumerate(animal)],
}), encoding="utf-8")

calls: dict[str, int] = {}
prompts: dict[str, list[str]] = {}
digest_tools: list = []
article_story_ids: list[str] = []
state_flags = {"article_calls": 0}
LIMIT_AFTER = int(os.environ.get("E2E_LIMIT_AFTER", "5"))


def clean_for(story: dict) -> str:
    return fx.make_article(story, words=WORDS, source_lines=fx.default_sources(catalog))


def story_of(prompt: str) -> dict:
    sid = re.search(r'"story_id": "([^"]+)"', prompt).group(1)
    title = re.search(r'"title": "([^"]+)"', prompt).group(1)
    return {"story_id": sid, "title": title, "prior_story_id": None}


def result_for(text: str, *, corpus: str | None = None, role: str = "x") -> llm.LLMResult:
    return llm.LLMResult(ok=True, text=text, model_used="fake", cost_usd=0.05, input_tokens=1000, output_tokens=len(text) // 4,
                         rate_limit=llm.RateLimitInfo(status="allowed", five_hour_utilization=0.2, seven_day_utilization=0.1),
                         tool_corpus=[corpus if corpus is not None else text])


def script(request: llm.LLMRequest):
    stage = request.stage
    calls[stage] = calls.get(stage, 0) + 1
    prompts.setdefault(stage, []).append(request.prompt)
    if stage == "digest":
        digest_tools.append(request.tools)
    if stage == "probe":
        if scenario == "org_limit":
            return llm.LLMResult(ok=False, error_kind="org_limit", error_detail="You've hit your org's monthly usage limit")
        return result_for("ok")
    if stage == "slate":
        if scenario == "slate_topup":  # the first answer has too few stories for any reserve; the second supplies them
            if calls["slate"] == 1:
                return result_for(json.dumps(fx.make_slate_json(11)))
            topics = ["otter holt abandonment after kelp loss", "lungfish aestivation genes", "wetland owl nest predation", "glacier frog chytrid prevalence"]
            extras = fx.make_slate_json(15)[11:]
            for entry, topic in zip(extras, topics):  # titles must differ from the chosen ones or the repeat check rightly drops them
                entry["title"] = f"Reserve story on {topic} in a monitored population"
            return result_for(json.dumps(extras))
        return result_for(json.dumps(fx.make_slate_json(13)))
    if stage in {"article", "repair"}:
        if stage == "article":
            state_flags["article_calls"] += 1
            article_story_ids.append(story_of(request.prompt)["story_id"])
            if scenario == "deadline_midway" and state_flags["article_calls"] == 7:
                clock["now"] = datetime(2026, 10, 26, 5, 0, tzinfo=timezone.utc)  # 14:00 JST, after the 11:30 deadline
            if scenario in {"limit_midway", "limit_partial"} and not second_pass and state_flags["article_calls"] > LIMIT_AFTER:
                return llm.LLMResult(ok=False, error_kind="usage_limit", error_detail="You've hit your session limit · resets 4:30pm (Asia/Tokyo)",
                                     reset_hint=datetime(2026, 10, 26, 7, 30, tzinfo=timezone.utc).timestamp())
        story = story_of(request.prompt) if stage == "article" else {
            "story_id": re.search(r"<!-- story: ([a-z0-9-]+)", request.prompt.split("# Current article")[1]).group(1),
            "title": re.search(r"(?m)^## (.+)$", request.prompt.split("# Current article")[1]).group(1), "prior_story_id": None}
        sid = story["story_id"]
        if scenario == "limit_in_repair" and stage == "repair" and not second_pass and sid == "fixture-story-2":
            return llm.LLMResult(ok=False, error_kind="usage_limit", error_detail="You've hit your session limit · resets 4:30pm (Asia/Tokyo)",
                                 reset_hint=datetime(2026, 10, 26, 7, 30, tzinfo=timezone.utc).timestamp())
        if scenario == "limit_in_repair" and stage == "article" and sid == "fixture-story-2":
            clean = fx.make_article(story, words=WORDS, source_lines=fx.default_sources(catalog))
            return result_for(clean.replace("### Counterpoint Matrix", "### Misc"), corpus=clean)
        if scenario == "dossier_quote" and stage == "article" and sid == "fixture-story-2":
            quoted = clean_for(story).replace("### Implications\n", '### Implications\n\nThe agency page states "the fixture population numbered 4,321 animals in the census year".\n')
            # the writer's own tool results back the article's figures but not the quotation: only the dossier page can
            return result_for(quoted, corpus=clean_for(story))
        if scenario == "taxonomy_typo" and stage == "article" and sid == "fixture-story-2":
            return result_for(fx.make_article(story, words=WORDS, source_lines=fx.default_sources(catalog), binomial="Genus speceis"),
                              corpus=clean_for(story))
        if scenario == "call_error" and stage == "article" and sid == "fixture-story-2":
            return llm.LLMResult(ok=False, error_kind="empty", error_detail="model returned no text")
        if scenario == "terminal_failed" and sid == "fixture-story-2":
            clean = fx.make_article(story, words=WORDS, source_lines=fx.default_sources(catalog))
            return result_for(clean.replace("### Implications\n", '### Implications\n\nOne author told reporters, "this exact sentence was never published in any source anywhere".\n'),
                              corpus=clean)
        if scenario == "skip_story" and stage == "article" and sid == "fixture-story-3":
            return result_for("SKIP: material_limited (only a press release exists)")
        clean = fx.make_article(story, words=WORDS, source_lines=fx.default_sources(catalog))
        if scenario == "messy_writer" and stage == "article" and sid == "fixture-story-2":
            return result_for(fx.make_article(story, words=WORDS, source_lines=fx.default_sources(catalog), messy=True), corpus=clean)
        if scenario == "missing_block" and stage == "article" and sid == "fixture-story-2":
            return result_for(clean.replace("### Counterpoint Matrix", "### Misc"), corpus=clean)
        if scenario == "invented_quote" and stage == "article" and sid == "fixture-story-2":
            quoted = clean.replace("### Implications\n", '### Implications\n\nOne author told reporters, "this exact sentence was never published in any source anywhere".\n')
            return result_for(quoted, corpus=clean)
        return result_for(clean, corpus=clean)
    if stage == "header":
        titles = re.findall(r'"title": "([^"]+)"', request.prompt)
        if scenario == "header_garbage":
            return result_for("I could not do this.")
        body = " ".join(f"Sentence {i} about the telemetry findings and what they mean for management." for i in range(45))
        refs = "\n".join(f'- Both stories turn on telemetry. Cross-reference: "{t}"' for t in titles[:2])
        return result_for(f"## Executive Summary\n\n{body}\n\n### Cross-currents\n\n{refs}\n")
    if stage == "digest":
        ids = re.findall(r'"story_id": "([^"]+)"', request.prompt)[:3]
        items = []
        for sid in ids:
            items.append(f"## Digest item for {sid} describing a smaller finding\n<!-- story: {sid} -->\n\n**Bottom line:** A modest but real result.\n\n"
                         + " ".join(["The survey recorded a measurable change in occupancy across 24 sites over two seasons."] * 14)
                         + f"\n\n### Primary Sources\n\n- First source {catalog[0]}\n- Second source {catalog[1]}\n")
        return result_for("\n\n".join(items))
    if stage == "review":
        return result_for("[]")
    raise AssertionError(f"unexpected stage {stage}")


deliver_log = root / "deliver-args.json"
deliver_script = root / "fake_deliver.py"
deliver_script.write_text(
    "import json, sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\nprint('PDF SHA256: x')\nprint('Email sent')\n", encoding="utf-8")
os.environ["BRIEFING_DELIVER_COMMAND"] = json.dumps([sys.executable, str(deliver_script), str(deliver_log), "--pdf", "{pdf}", "--date", "{date}",
                                                     "--edition", "{edition}", "--body-text-file", "{body_text}", "--body-html-file", "{body_html}",
                                                     "--subject", "{subject}", "--recipient", "{recipients}"])
os.environ["ANIMAL_BRIEFING_RECIPIENTS"] = "a@example.com,b@example.com"
if scenario not in {"held", "late_standalone", "release_held"}:
    os.environ["BRIEFING_FORCE_STANDALONE"] = "1"  # the edition normally rides inside the daily briefing; most scenarios test the stand-alone path
else:
    daily_root = root / "daily-sibling"
    os.environ["BRIEFING_DAILY_ROOT"] = str(daily_root)
    (daily_root / "generated").mkdir(parents=True, exist_ok=True)
    if scenario == "late_standalone":
        (daily_root / "generated" / f"claude-daily-briefing-{DATE}-conductor.json").write_text(
            json.dumps({"date": DATE, "edition": "daily", "delivered": {"articles": 21}}), encoding="utf-8")
        (daily_root / "generated" / f"claude-daily-briefing-{DATE}-combined.json").write_text(json.dumps({"included": False, "reason": "it had not finished"}), encoding="utf-8")

now = datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc)  # 05:00 JST on the briefing date
if scenario == "past_deadline":
    now = datetime(2026, 10, 26, 5, 0, tzinfo=timezone.utc)  # 14:00 JST, after the 11:30 deadline
if scenario == "release_held" and second_pass:
    now = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)  # 19:00 JST: the daily briefing's own deadline has long passed
clock = {"now": now}

fetch_counter = {"n": 0}


class UniqueCommons(FakeFetcher):
    def get_json(self, url, params=None):
        if "wikimedia" in url:
            fetch_counter["n"] += 1
            self.commons = [commons_page(f"Photo {fetch_counter['n']}-{i}.jpg", description="Example species Genus species in its habitat") for i in range(12)]
        return super().get_json(url, params)


class FakeGbifWeb(FakeWeb):
    """Serves the fixture slate's source pages and a GBIF that knows ``Genus species`` (VU) and spots the typo."""

    def get_json(self, url, params=None):
        if "/species/match" in url:
            name = params["name"]
            if name in {"Genus species", "Genus speceis"}:
                exact = name == "Genus species"
                return {"matchType": "EXACT" if exact else "FUZZY", "canonicalName": "Genus species", "status": "ACCEPTED", "rank": "SPECIES", "usageKey": 7}
            return {"matchType": "NONE"}
        if "iucnRedListCategory" in url:
            return {"code": "VU", "category": "VULNERABLE"}
        raise RuntimeError("no fixture")


web_fetcher = None
if scenario in {"dossier", "dossier_quote", "taxonomy_typo"}:
    page = ("<html><head><title>Fixture agency page</title></head><body><article><p>The fixture population numbered 4,321 animals in the census year, "
            "according to the agency, and monitoring continued through the following survey season at all of the long-term plots. "
            "The agency added that detection probability was modelled separately for every transect and that the full dataset is archived "
            "in a public repository with the analysis scripts.</p></article></body></html>")
    web_fetcher = FakeGbifWeb({f"https://example.org/{kind}-{i}": html_bytes(page) for i in range(14) for kind in ("paper", "agency")})

settings = {"slate_reserves": 4 if scenario == "slate_topup" else 3, "review_enabled": True, "models": {"review": {"model": "opus", "effort": "low", "optional": True}}}
conductor = cc.Conductor(
    edition=edition, date=DATE, scripts_dir=SCRIPTS, backend=llm.FakeBackend(script), photo_fetcher=UniqueCommons(), web_fetcher=web_fetcher,
    check_network=False, audit_fixture=audit_fixture, now=lambda: clock["now"], sleep=lambda s: None, settings_override=settings,
)
if scenario == "crash_early":
    def _boom(self):
        raise RuntimeError("simulated crash before any article")

    cc.Conductor.stage_photos = _boom
if scenario == "crash_late" and not second_pass:
    def _boom_pdf(self):
        raise RuntimeError("simulated crash while building the PDF")

    cc.Conductor.stage_pdf = _boom_pdf
if scenario == "busy":
    conductor.lock.path.parent.mkdir(parents=True, exist_ok=True)
    conductor.lock.path.write_text(json.dumps({"pid": os.getppid(), "started_epoch": __import__("time").time()}), encoding="utf-8")
if scenario in {"interrupted", "terminal_failed", "call_error"} and not second_pass:
    # Simulate a run that drafted everything and then died before the last mile (power loss, killed task).
    def _crash(self, *, degraded):
        raise cc.Deferred("simulated interruption before the last mile")

    cc.Conductor._finish = _crash
code = conductor.run(finish_only="--finish" in sys.argv)

gen = Path(conductor.generated_dir)
arch = Path(conductor.archive_dir)
stem = conductor.ctx.profile.artifact_stem
out = {
    "exit": code, "calls": calls, "article_story_ids": article_story_ids,
    "articles_done": sorted(p.stem for p in conductor.articles_dir.glob("*.md") if not p.stem.endswith((".raw", ".last"))),
    "pdf_exists": (gen / f"{stem}-{DATE}.pdf").exists(),
    "assembled_exists": (arch / f"{stem}-{DATE}.md").exists(),
    "alerts": sorted(p.name for p in (gen / "alerts").glob("*")) if (gen / "alerts").exists() else [],
    "delivered": bool(conductor.state.state.get("delivered")),
    "deliver_args": json.loads(deliver_log.read_text()) if deliver_log.exists() else None,
    "stages": {k: v.get("status") for k, v in conductor.state.state["stages"].items() if not k.startswith("article:")},
    "notes": conductor.state.state.get("delivery_notes"),
    "spent": conductor.state.state.get("spent_usd"),
    "events": [e["message"] for e in conductor.state.state["events"]][-25:],
    "repair_prompts": [p.split("# What failed")[1].split("# Instructions")[0] for p in prompts.get("repair", [])],
    "header_source": conductor.state.state["stages"].get("header", {}).get("source"),
    "run_status": conductor.state.state["stages"].get("run", {}).get("status"),
    "delivered_info": conductor.state.state.get("delivered"),
    "email_files": sorted(p.name for p in gen.glob("*-email.*")),
    "digest_tools": digest_tools,
    "article_prompts": prompts.get("article", [])[:3],
    "dossier_files": sorted(p.name for p in conductor.articles_dir.glob("*.dossier*.json")),
    "taxonomy_meta": json.loads((conductor.articles_dir / "fixture-story-2.json").read_text()).get("provenance", {}).get("taxonomy")
    if (conductor.articles_dir / "fixture-story-2.json").exists() else None,
    "lock_left_behind": conductor.lock.path.exists(),
}
pdf = gen / f"{stem}-{DATE}.pdf"
if pdf.exists():
    from pypdf import PdfReader

    reader = PdfReader(str(pdf))
    out["pdf_page1"] = reader.pages[0].extract_text()[:400]
    out["pdf_pages"] = len(reader.pages)
    out["pdf_images"] = sum(len(page.images) for page in reader.pages)
assembled = arch / f"{stem}-{DATE}.md"
out["assembled_text"] = assembled.read_text(encoding="utf-8")[:200000] if assembled.exists() else ""
import run_reporting  # noqa: E402

usage = (run_reporting.load_run_report(DATE).get("usage") or {}).get("totals") or {}
out["report_run_status"] = run_reporting.load_run_report(DATE).get("run_status")
_report = run_reporting.load_run_report(DATE)
out["visual_qa_status"] = (_report.get("timings") or {}).get("visual_qa", {}).get("status")
out["gate"] = list(run_reporting.stage_prerequisites_satisfied(_report, "delivery"))
out["usage_calls"] = usage.get("calls")
out["usage_cost"] = usage.get("cost_usd")
out["accounting_errors"] = [e["message"] for e in conductor.state.state["events"] if "accounting failed" in e["message"]]
print("E2E_RESULT " + json.dumps(out))
