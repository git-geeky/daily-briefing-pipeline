"""Shared SMTP delivery for the daily briefing pipeline.

Designed for a host where the SMTP credential file and the transferred PDF live.
Runner wrappers can invoke this same module on their delivery host.

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
contract the Claude SKILL.md Step 5 originally required). A resend for an
already-delivered (runner, edition, date) is suppressed by a receipt file and still prints
`Email sent` (with a note) so retried scheduled runs stay idempotent.

Hardening over the original script: verified TLS context, bounded retries with
backoff for transient SMTP/network errors (never for authentication failures),
multi-recipient support (comma-separated `--recipient` / `recipients` in config),
an explicit sender account (`sender`, falling back to `default_recipient`), optional
per-edition overrides under `editions.<id>` in the config, and an optional HTML body.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import smtplib
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path
from typing import Callable, Iterable

try:
    from private_config import load_json_with_private_overlay
except ImportError:  # pragma: no cover - supports package-style imports
    try:
        from .private_config import load_json_with_private_overlay
    except ImportError:  # the delivery host may hold only this file and its JSON config: no private overlay there

        def load_json_with_private_overlay(path, overlay_key=None):  # type: ignore[misc]
            return json.loads(Path(path).read_text(encoding="utf-8"))


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "delivery-config.json"


def load_config(path: Path | None = None) -> dict:
    target = path or DEFAULT_CONFIG_PATH
    if not target.exists():
        raise SystemExit(f"delivery-config.json not found at {target}")
    return load_json_with_private_overlay(target, overlay_key="delivery_config")


def human_date(iso_date: str) -> str:
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    return dt.strftime("%A, %B %-d, %Y") if sys.platform != "win32" else dt.strftime("%A, %B %#d, %Y")


def parse_recipients(value: str | Iterable[str] | None) -> list[str]:
    """Normalise ``a@x.com, b@y.com`` (or a list) into a de-duplicated list of addresses."""
    if value is None:
        return []
    raw = value.replace(";", ",").split(",") if isinstance(value, str) else [str(item) for item in value]
    seen: dict[str, None] = {}
    for item in raw:
        address = item.strip()
        if address and "@" in address:
            seen.setdefault(address, None)
    return list(seen)


def _edition_config(config: dict, edition: str | None) -> dict:
    """Overlay ``editions.<id>`` keys on the base config (shallow, runner_labels merged)."""
    if not edition:
        return config
    overlay = (config.get("editions") or {}).get(edition)
    if not isinstance(overlay, dict):
        return config
    merged = {**config, **{key: value for key, value in overlay.items() if key != "runner_labels"}}
    merged["runner_labels"] = {**config.get("runner_labels", {}), **(overlay.get("runner_labels") or {})}
    return merged


def _sender_and_recipients(config: dict, recipient: str | Iterable[str] | None,
                           also: str | Iterable[str] | None = None) -> tuple[str, list[str]]:
    sender = str(config.get("sender") or config.get("default_recipient") or "").strip()
    if not sender:
        raise SystemExit("delivery config has neither 'sender' nor 'default_recipient'")
    recipients = (
        parse_recipients(recipient)
        or parse_recipients(config.get("recipients"))
        or parse_recipients(config.get("default_recipient"))
    )
    if not recipients:
        raise SystemExit("no recipient: pass --recipient or set 'recipients'/'default_recipient' in delivery config")
    # "Also" addresses are added to whoever would receive it anyway, so extending the list can never drop the owner.
    recipients = parse_recipients([*recipients, *parse_recipients(also)])
    return sender, recipients


def _read_app_password(config: dict) -> str:
    path = str(config.get("password_path_on_aiserver") or "").strip()
    if not path:
        raise SystemExit("delivery config has no 'password_path_on_aiserver'")
    try:
        password = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"cannot read SMTP app password file: {exc}") from exc
    if not password:
        raise SystemExit("SMTP app password file is empty")
    return password


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused)):
        return False
    if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, socket.timeout, TimeoutError)):
        return True
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    return isinstance(exc, OSError)


def _smtp_send(
    config: dict,
    msg: MIMEMultipart,
    *,
    sender: str,
    recipients: list[str],
    password: str,
    retries: int = 3,
    backoff: float = 2.0,
    smtp_factory: Callable[..., smtplib.SMTP] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Send with STARTTLS (verified context). Retry transient failures, fail fast on auth/recipient errors."""
    factory = smtp_factory or smtplib.SMTP
    context = ssl.create_default_context()
    attempt = 0
    while True:
        attempt += 1
        try:
            with factory(config["smtp_host"], int(config["smtp_port"]), timeout=30) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(sender, password)
                server.send_message(msg, from_addr=sender, to_addrs=recipients)
            return
        except smtplib.SMTPAuthenticationError as exc:
            raise SystemExit(
                f"SMTP authentication failed ({exc.smtp_code}): the app password is wrong or revoked; not retrying"
            ) from exc
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            raise SystemExit(f"SMTP refused the sender/recipients: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - classified below
            if attempt > retries or not _is_transient(exc):
                raise SystemExit(f"SMTP send failed after {attempt} attempt(s): {exc!r}") from exc
            delay = backoff * (2 ** (attempt - 1))
            print(f"SMTP attempt {attempt} failed ({exc!r}); retrying in {delay:.0f}s", file=sys.stderr)
            sleep(delay)


def _receipt_path(receipt_dir: Path, runner: str, edition: str | None, date: str) -> Path:
    """One receipt per (runner, edition, date). Deliberately *not* keyed on the PDF's hash: the PDF is rebuilt on a resumed
    run and its bytes differ every time, so a hash key lets an ambiguous first send (reply lost after the message went out)
    be followed by a second, different-looking copy. ``--force`` is the override for a deliberate re-send."""
    safe = lambda text: "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(text))  # noqa: E731
    return receipt_dir / f"{safe(runner.lower())}.{safe(edition or 'default')}.{safe(date)}.delivered.json"


def _resolve_receipt_dir(config: dict, override: Path | None) -> Path:
    if override:
        return override
    configured = config.get("receipt_dir")
    if configured:
        return Path(str(configured)).expanduser()
    return Path(os.environ.get("BRIEFING_DELIVERY_RECEIPT_DIR") or Path.home() / ".briefing-delivery-receipts")


def _already_delivered(receipt: Path, recipients: list[str]) -> dict | None:
    if not receipt.exists():
        return None
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if set(recipients) <= set(payload.get("recipients") or []):
        return payload
    return None


def send_briefing_email(
    *,
    pdf_path: Path,
    runner: str,
    date: str,
    recipient: str | Iterable[str] | None = None,
    subject_override: str | None = None,
    config_path: Path | None = None,
    edition: str | None = None,
    body_text: str | None = None,
    body_html: str | None = None,
    receipt_dir: Path | None = None,
    force: bool = False,
    retries: int = 3,
    also_recipient: str | Iterable[str] | None = None,
    smtp_factory: Callable[..., smtplib.SMTP] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Send one briefing PDF via SMTP.

    Raises SystemExit on any failure so callers don't accidentally suppress errors.
    Prints the literal string `Email sent` on success (preserving the contract
    that the Claude SKILL.md originally required at Step 5).
    """
    config = _edition_config(load_config(config_path), edition)
    sender, recipients = _sender_and_recipients(config, recipient, also_recipient)
    runner_labels: dict = config.get("runner_labels", {})
    label = runner_labels.get(runner.lower(), runner.title())

    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")
    pdf_size = pdf_path.stat().st_size
    floor = int(config.get("min_pdf_bytes", 100 * 1024))
    if pdf_size < floor:
        raise SystemExit(f"PDF is suspiciously small ({pdf_size} bytes < {floor} byte floor): {pdf_path}")

    pdf_bytes = pdf_path.read_bytes()
    pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    print(f"PDF SHA256: {pdf_sha256}")

    receipts = _resolve_receipt_dir(config, receipt_dir)
    receipt = _receipt_path(receipts, runner, edition, date)
    prior = None if force else _already_delivered(receipt, recipients)
    if prior:
        print(f"Email sent (already delivered at {prior.get('delivered_at')}; duplicate send suppressed, pass --force to send again)")
        return {**prior, "duplicate_suppressed": True}

    app_password = _read_app_password(config)
    h_date = human_date(date)
    fields = {"runner_label": label, "human_date": h_date, "pdf_sha256": pdf_sha256, "pdf_size_bytes": pdf_size}
    subject = subject_override or config["subject_template"].format(**fields)
    text = body_text if body_text is not None else config.get(
        "body_template", "{runner_label} briefing for {human_date}."
    ).format(**fields)

    msg = MIMEMultipart("mixed")
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    if body_html:
        alternative = MIMEMultipart("alternative")
        alternative.attach(MIMEText(text, "plain", "utf-8"))
        alternative.attach(MIMEText(body_html, "html", "utf-8"))
        msg.attach(alternative)
    else:
        msg.attach(MIMEText(text, "plain", "utf-8"))

    part = MIMEBase("application", "pdf")
    part.set_payload(pdf_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=pdf_path.name)
    msg.attach(part)

    _smtp_send(
        config, msg, sender=sender, recipients=recipients, password=app_password,
        retries=retries, smtp_factory=smtp_factory, sleep=sleep,
    )

    delivered_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    result = {
        "sha256": pdf_sha256,
        "size_bytes": pdf_size,
        "subject": subject,
        "recipient": recipients[0],
        "recipients": recipients,
        "delivered_at": delivered_at,
        "edition": edition,
        "date": date,
    }
    try:
        receipts.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:  # delivery succeeded; losing the receipt only risks a duplicate on retry
        print(f"warning: could not write delivery receipt {receipt}: {exc}", file=sys.stderr)
    print("Email sent")
    return result


def send_plaintext_email(
    *,
    runner: str,
    subject: str,
    body: str,
    recipient: str | Iterable[str] | None = None,
    config_path: Path | None = None,
    edition: str | None = None,
    retries: int = 3,
    smtp_factory: Callable[..., smtplib.SMTP] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Send a plaintext-only email (no PDF attachment). Used by the Fleet Diff summary and alerts.

    `subject` is used verbatim — the `subject_template` from config is NOT applied.
    Callers should format the subject themselves for non-standard deliveries.
    """
    config = _edition_config(load_config(config_path), edition)
    sender, recipients = _sender_and_recipients(config, recipient)
    app_password = _read_app_password(config)

    msg = MIMEMultipart("mixed")
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg.attach(MIMEText(body, "plain", "utf-8"))

    _smtp_send(
        config, msg, sender=sender, recipients=recipients, password=app_password,
        retries=retries, smtp_factory=smtp_factory, sleep=sleep,
    )

    print("Email sent")
    return {
        "subject": subject,
        "recipient": recipients[0],
        "recipients": recipients,
        "delivered_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdf", type=Path, help="Path to the PDF to send.")
    parser.add_argument("--runner", required=True, help="Runner label key (claude, codex, consensus, diff).")
    parser.add_argument("--date", help="Briefing date in YYYY-MM-DD (required for PDF sends).")
    parser.add_argument("--recipient", help="Override recipient(s); comma-separated for several.")
    parser.add_argument("--edition", help="Apply the config's editions.<id> overrides (subject/body/recipients).")
    parser.add_argument("--also-recipient", help="Extra recipient(s), comma-separated, added to the configured or --recipient ones.")
    parser.add_argument("--body-text-file", type=Path, help="Plaintext body for a PDF send (replaces body_template).")
    parser.add_argument("--body-html-file", type=Path, help="HTML body for a PDF send (sent as multipart/alternative).")
    parser.add_argument("--receipt-dir", type=Path, help="Where delivery receipts live (default: ~/.briefing-delivery-receipts).")
    parser.add_argument("--force", action="store_true", help="Send even if a receipt shows this edition and date was already delivered.")
    parser.add_argument("--retries", type=int, default=3, help="Retries for transient SMTP errors (default 3).")
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
            edition=args.edition,
            retries=args.retries,
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
        edition=args.edition,
        body_text=args.body_text_file.read_text(encoding="utf-8") if args.body_text_file else None,
        body_html=args.body_html_file.read_text(encoding="utf-8") if args.body_html_file else None,
        receipt_dir=args.receipt_dir,
        force=args.force,
        retries=args.retries,
        also_recipient=args.also_recipient,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
