"""Shared SMTP delivery for the daily briefing pipeline.

Designed to run on aiserver (where the Gmail app password and the transferred PDF
live). Both the Claude and Codex runners eventually invoke this same module via
`ssh aiserver python3 /home/agent/briefing-pipeline/deliver_core.py ...`.

Config lives alongside this module in `delivery-config.json`.

Invocation:
    python3 deliver_core.py \
        --pdf /tmp/claude-daily-briefing-2026-04-22.pdf \
        --runner claude \
        --date 2026-04-22

    python3 deliver_core.py \
        --pdf /tmp/codex-daily-briefing-2026-04-22.pdf \
        --runner codex \
        --date 2026-04-22

The `--runner` flag drives the subject-line label via `delivery-config.json`'s
`runner_labels` map.

Success indicator: the literal string `Email sent` on stdout (preserves the
contract the Claude SKILL.md Step 5 originally required).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import smtplib
import sys
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "delivery-config.json"


def load_config(path: Path | None = None) -> dict:
    target = path or DEFAULT_CONFIG_PATH
    if not target.exists():
        raise SystemExit(f"delivery-config.json not found at {target}")
    return json.loads(target.read_text(encoding="utf-8"))


def human_date(iso_date: str) -> str:
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    return dt.strftime("%A, %B %-d, %Y") if sys.platform != "win32" else dt.strftime("%A, %B %#d, %Y")


def send_briefing_email(
    *,
    pdf_path: Path,
    runner: str,
    date: str,
    recipient: str | None = None,
    subject_override: str | None = None,
    config_path: Path | None = None,
) -> dict:
    """Send one briefing PDF via Gmail SMTP.

    Raises SystemExit on any failure so callers don't accidentally suppress errors.
    Prints the literal string `Email sent` on success (preserving the contract
    that the Claude SKILL.md originally required at Step 5).
    """
    config = load_config(config_path)
    recipient = recipient or config["default_recipient"]
    runner_labels: dict = config.get("runner_labels", {})
    label = runner_labels.get(runner.lower(), runner.title())

    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")
    pdf_size = pdf_path.stat().st_size
    if pdf_size < 100 * 1024:
        raise SystemExit(f"PDF is suspiciously small ({pdf_size} bytes < 100 KB floor): {pdf_path}")

    app_password = Path(config["password_path_on_aiserver"]).read_text().strip()
    if not app_password:
        raise SystemExit("Gmail app password file is empty")

    pdf_bytes = pdf_path.read_bytes()
    pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()

    h_date = human_date(date)
    subject = subject_override or config["subject_template"].format(
        runner_label=label, human_date=h_date
    )
    body_text = config.get("body_template", "{runner_label} briefing for {human_date}.").format(
        runner_label=label,
        human_date=h_date,
        pdf_sha256=pdf_sha256,
        pdf_size_bytes=pdf_size,
    )

    msg = MIMEMultipart()
    msg["From"] = recipient
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain"))

    part = MIMEBase("application", "pdf")
    part.set_payload(pdf_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=pdf_path.name)
    msg.attach(part)

    print(f"PDF SHA256: {pdf_sha256}")

    with smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=30) as server:
        server.starttls()
        server.login(recipient, app_password)
        server.send_message(msg)

    print("Email sent")
    return {
        "sha256": pdf_sha256,
        "size_bytes": pdf_size,
        "subject": subject,
        "recipient": recipient,
        "delivered_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def send_plaintext_email(
    *,
    runner: str,
    subject: str,
    body: str,
    recipient: str | None = None,
    config_path: Path | None = None,
) -> dict:
    """Send a plaintext-only email (no PDF attachment). Used by the Fleet Diff summary.

    `subject` is used verbatim — the `subject_template` from config is NOT applied.
    Callers should format the subject themselves for non-standard deliveries.
    """
    config = load_config(config_path)
    recipient = recipient or config["default_recipient"]
    app_password = Path(config["password_path_on_aiserver"]).read_text().strip()

    msg = MIMEMultipart()
    msg["From"] = recipient
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=30) as server:
        server.starttls()
        server.login(recipient, app_password)
        server.send_message(msg)

    print("Email sent")
    return {
        "subject": subject,
        "recipient": recipient,
        "delivered_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdf", type=Path, help="Path to the PDF to send.")
    parser.add_argument("--runner", required=True, help="Runner label key (claude, codex, consensus, diff).")
    parser.add_argument("--date", help="Briefing date in YYYY-MM-DD (required for PDF sends).")
    parser.add_argument("--recipient", help="Override the recipient email address.")
    parser.add_argument("--subject", help="Override the computed subject line.")
    parser.add_argument("--config", type=Path, help="Path to delivery-config.json (default: next to this script).")
    parser.add_argument(
        "--body-from-stdin",
        action="store_true",
        help="Send a plaintext email where the body is read from stdin and --pdf is ignored.",
    )
    args = parser.parse_args()

    if args.body_from_stdin:
        if not args.subject:
            parser.error("--subject is required with --body-from-stdin")
        body = sys.stdin.read()
        send_plaintext_email(
            runner=args.runner,
            subject=args.subject,
            body=body,
            recipient=args.recipient,
            config_path=args.config,
        )
        return 0

    if not args.pdf or not args.date:
        parser.error("--pdf and --date are required unless --body-from-stdin is set")

    send_briefing_email(
        pdf_path=args.pdf,
        runner=args.runner,
        date=args.date,
        recipient=args.recipient,
        subject_override=args.subject,
        config_path=args.config,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
