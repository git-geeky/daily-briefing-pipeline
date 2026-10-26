"""Subprocess harness for the DAILY edition: the real conductor against a scripted fake model.

Usage: python e2e_harness_daily.py <scenario> <root>
Prints one line ``E2E_RESULT {json}`` for the test to assert on.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
REPO = HERE.parents[2]
for entry in (str(HERE), str(REPO), str(SCRIPTS)):
    sys.path.insert(0, entry)

scenario, root = sys.argv[1], Path(sys.argv[2])
os.environ.update({"BRIEFING_EDITION": "daily", "DAILY_BRIEFING_ROOT": str(root), "DAILY_BRIEFING_SHARED_ROOT": str(REPO)})
DATE = "2026-10-26"

import animal_fixtures as fx  # noqa: E402
import conductor_core as cc  # noqa: E402
import daily_fixtures as dfx  # noqa: E402
import edition_core  # noqa: E402
import llm_backend_core as llm  # noqa: E402
import source_audit_core as audit  # noqa: E402

edition = edition_core.load_edition("daily")
WORDS = int(os.environ.get("E2E_WORDS", "1600"))

fixture = root / "audit-fixture.json"
fixture.write_text(json.dumps({"default_status": "checked_no_relevant_update"}), encoding="utf-8")

calls: dict[str, int] = {}
prompts: dict[str, list[str]] = {}


def ok(text: str) -> llm.LLMResult:
    return llm.LLMResult(ok=True, text=text, model_used="fake", cost_usd=0.05, input_tokens=1000, output_tokens=len(text) // 4,
                         rate_limit=llm.RateLimitInfo(status="allowed", five_hour_utilization=0.2, seven_day_utilization=0.1),
                         tool_corpus=[text])


def script(request: llm.LLMRequest):
    stage = request.stage
    calls[stage] = calls.get(stage, 0) + 1
    prompts.setdefault(stage, []).append(request.prompt)
    if stage == "probe":
        return ok("ok")
    if stage == "slate":
        slate = dfx.make_daily_slate(part4=1 if scenario == "short_section" else 4)
        if scenario == "no_decision":
            slate["no_update_decisions"] = []
        return ok(json.dumps(slate))
    if stage in {"article", "repair"}:
        if stage == "article":
            story = {"story_id": re.search(r'"story_id": "([^"]+)"', request.prompt).group(1),
                     "title": re.search(r'"title": "([^"]+)"', request.prompt).group(1),
                     "prior_story_id": (re.search(r'"prior_story_id": "([^"]+)"', request.prompt) or [None, None])[1]}
        else:
            story = {"story_id": re.search(r"<!-- story: ([a-z0-9-]+)", request.prompt.split("# Current article")[1]).group(1),
                     "title": re.search(r"(?m)^## (.+)$", request.prompt.split("# Current article")[1]).group(1), "prior_story_id": None}
        section = story["story_id"].split("-story-")[0]
        text = dfx.make_daily_article(story, section, words=WORDS)
        if scenario == "single_counterpoint_row" and stage == "article" and story["story_id"] == "part1-story-1":
            text = "\n".join(line for line in text.splitlines() if not line.startswith("| The change is temporary"))
        return ok(text)
    if stage == "header":
        pairs = re.findall(r'"title": "([^"]+)",\s*"bottom_line": "[^"]*",\s*"confidence": "[^"]*",\s*"group": "[^"]*",\s*"story_id": "[^"]*",\s*"region": "[^"]*",\s*"taxa": "[^"]*",\s*"part": "([^"]*)"', request.prompt)
        if not pairs:
            pairs = [(t, "") for t in re.findall(r'"title": "([^"]+)"', request.prompt)]
        body = " ".join(f"Sentence {i} describes how the day's developments connect across sections." for i in range(70))
        refs = "\n".join(f'- Force {i + 1} links these stories. Cross-reference: {part + " - " if part else ""}"{title}"'
                         for i, (title, part) in enumerate(pairs[:3]))
        return ok(f"## Executive Summary\n\n{body}\n\n### Top 3 Structural Forces\n\n{refs}\n")
    if stage == "closing":
        if scenario == "closing_garbage":
            return ok("I cannot do this.")
        urls = re.findall(r"https://[^\s\"\\,\]]+", request.prompt.split("# Inputs")[1])
        urls = [u for u in dict.fromkeys(urls) if "example.org" not in u][:5]
        text = "## What the day's stories share about institutions under pressure\n\n" + fx.filler_paragraphs(500, "c1") + \
               "\n\n### What to Watch\n\n" + fx.filler_paragraphs(300, "c2") + "\n\n### Default Skepticism Flags\n\n" + \
               fx.filler_paragraphs(400, "c3") + "\n\n### Primary Sources\n\n" + "\n".join(f"- Source {i}. {u}" for i, u in enumerate(urls))
        return ok(text)
    raise AssertionError(f"unexpected stage {stage}")


def runner(argv, *, env, cwd, timeout):
    if any(str(part).endswith("fetch_briefing_images.py") for part in argv):
        return 0, "stubbed: no network image fetch in tests"
    return cc.default_runner(argv, env=env, cwd=cwd, timeout=timeout)


deliver_log = root / "deliver-args.json"
deliver_script = root / "fake_deliver.py"
deliver_script.write_text("import json, sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\nprint('Email sent')\n", encoding="utf-8")
os.environ["BRIEFING_DELIVER_COMMAND"] = json.dumps([sys.executable, str(deliver_script), str(deliver_log), "--pdf", "{pdf}", "--date", "{date}",
                                                     "--edition", "{edition}", "--body-text-file", "{body_text}", "--body-html-file", "{body_html}",
                                                     "--subject", "{subject}", "--recipient", "{recipients}",
                                                     "--also-recipient", "{extra_recipients}"])
os.environ.pop("DAILY_BRIEFING_RECIPIENTS", None)
os.environ["DAILY_BRIEFING_EXTRA_RECIPIENTS"] = "extra@example.com,second@example.com"

if scenario in {"combined", "combined_corrupt"}:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    animal_gen = root / "animal" / "generated"
    animal_gen.mkdir(parents=True, exist_ok=True)
    animal_pdf = animal_gen / f"claude-animal-briefing-{DATE}.pdf"
    if scenario == "combined_corrupt":
        animal_pdf.write_bytes(b"this is not a pdf")
    else:
        sheet = canvas.Canvas(str(animal_pdf), pagesize=A4)
        for n in range(3):
            sheet.drawString(60, 780, f"ANIMAL SAMPLE PAGE {n + 1}")
            sheet.showPage()
        sheet.save()
    (animal_gen / f"claude-animal-briefing-{DATE}-email.txt").write_text(
        "The Animal Science Briefing - Monday, October 26, 2026\n\n2 stories in depth:\n\n1. Numbats rebound in Dryandra\n   Bottom line text.\n\n2. A second animal story\n", encoding="utf-8")
    (animal_gen / f"claude-animal-briefing-{DATE}-email.html").write_text("<h2>The Animal Science Briefing</h2><ol><li>Numbats rebound in Dryandra</li></ol>", encoding="utf-8")
    (animal_gen / f"claude-animal-briefing-{DATE}-conductor.json").write_text(json.dumps(
        {"date": DATE, "edition": "animal", "delivered": {"held": "carried inside the daily briefing", "articles": 2}}), encoding="utf-8")

conductor = cc.Conductor(
    edition=edition, date=DATE, scripts_dir=SCRIPTS, backend=llm.FakeBackend(script), runner=runner, check_network=False,
    audit_fixture=fixture, now=lambda: datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc), sleep=lambda s: None,
)
code = conductor.run()

gen, arch = Path(conductor.generated_dir), Path(conductor.archive_dir)
stem = conductor.ctx.profile.artifact_stem
assembled = arch / f"{stem}-{DATE}.md"
pdf = gen / f"{stem}-{DATE}.pdf"
out = {
    "exit": code, "calls": calls, "delivered": bool(conductor.state.state.get("delivered")), "pdf_exists": pdf.exists(),
    "articles_done": sorted(p.stem for p in conductor.articles_dir.glob("*.md") if not p.stem.endswith(".raw")),
    "stages": {k: v.get("status") for k, v in conductor.state.state["stages"].items() if not k.startswith("article:")},
    "short_sections": conductor.state.state["stages"].get("articles", {}).get("short_sections"),
    "notes": conductor.state.state.get("delivery_notes"), "events": [e["message"] for e in conductor.state.state["events"]][-30:],
    "repair_findings": [p.split("# What failed")[1].split("# Instructions")[0] for p in prompts.get("repair", [])],
    "header_source": conductor.state.state["stages"].get("header", {}).get("source"),
    "combined_marker": json.loads(conductor._combined_marker_path().read_text()) if conductor._combined_marker_path().exists() else None,
    "closing_fallback": conductor.state.state["stages"].get("closing", {}).get("fallback"),
    "assembled_text": assembled.read_text(encoding="utf-8") if assembled.exists() else "",
    "deliver_args": json.loads(deliver_log.read_text()) if deliver_log.exists() else None,
    "validation": json.loads((gen / f"{stem}-{DATE}-validation.json").read_text(encoding="utf-8")) if (gen / f"{stem}-{DATE}-validation.json").exists() else None,
}
if out["deliver_args"]:
    args = out["deliver_args"]
    out["email_text"] = Path(args[args.index("--body-text-file") + 1]).read_text(encoding="utf-8")
    out["email_html"] = Path(args[args.index("--body-html-file") + 1]).read_text(encoding="utf-8")
    sent_pdf = Path(args[args.index("--pdf") + 1])
    out["sent_pdf_name"] = sent_pdf.name
    if sent_pdf.exists():
        from pypdf import PdfReader

        reader = PdfReader(str(sent_pdf))
        out["sent_pdf_pages"] = len(reader.pages)
        out["sent_pdf_page1"] = reader.pages[0].extract_text()
        out["sent_pdf_page2"] = reader.pages[1].extract_text() if len(reader.pages) > 1 else ""
        out["daily_pdf_pages"] = len(PdfReader(str(pdf)).pages) if pdf.exists() else None
if out["validation"]:
    out["validation"] = {k: out["validation"].get(k) for k in ("blocking_errors", "warnings")}
import run_reporting  # noqa: E402

usage = (run_reporting.load_run_report(DATE).get("usage") or {}).get("totals") or {}
out["usage_calls"] = usage.get("calls")
out["usage_cost"] = usage.get("cost_usd")
out["accounting_errors"] = [e["message"] for e in conductor.state.state["events"] if "accounting failed" in e["message"]]
print("E2E_RESULT " + json.dumps(out))
