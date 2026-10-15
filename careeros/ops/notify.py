"""Windows notification after the daily run, so new jobs worth a look do not wait for you to open the report.

The title and text reach PowerShell only through environment variables and are XML-escaped there: posting text
is untrusted and is never placed in the command line. A failed notification is logged; it never fails the run.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger("careeros")

# Windows PowerShell's own AppUserModelID: toasts need a registered app identity, and this one always exists.
APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$t = [System.Security.SecurityElement]::Escape($env:CAREEROS_TOAST_TITLE)
$b = [System.Security.SecurityElement]::Escape($env:CAREEROS_TOAST_BODY)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$b</text></binding></visual></toast>")
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:CAREEROS_TOAST_APP).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""
TITLE_LIMIT, BODY_LIMIT = 120, 400


def summary(store, since: str) -> tuple[str, str] | None:
    """(title, text) for jobs and email leads found since `since`, or None when there is nothing worth a ping."""
    jobs = store.all("""SELECT j.title, j.employer_name, s.tier FROM jobs j JOIN scores s ON s.score_id =
                        (SELECT score_id FROM scores WHERE job_id = j.job_id ORDER BY at DESC, rowid DESC LIMIT 1)
                        WHERE j.current_state = 'scored' AND s.tier IN ('A','B','near_miss') AND j.first_seen_at >= ?
                          AND j.variant_of IS NULL ORDER BY s.apply_priority DESC""", (since,))
    leads = store.one("SELECT COUNT(*) c FROM leads WHERE status = 'new' AND family IS NOT NULL AND first_seen_at >= ?", (since,))["c"]
    night = store.one("""SELECT COUNT(DISTINCT a.job_id) c FROM night_results a JOIN jobs j ON j.job_id = a.job_id
                         WHERE a.at >= ? AND j.first_seen_at >= ? AND a.tier IN ('A','B','near_miss')""", (since, since))["c"]
    if not jobs and not leads and not night:
        return None
    parts = []
    if jobs:
        parts.append(f"{len(jobs)} new job{'s' if len(jobs) != 1 else ''} worth a look")
    if night:
        parts.append(f"{night} NIGHT job{'s' if night != 1 else ''}")
    if leads:
        parts.append(f"{leads} new email lead{'s' if leads != 1 else ''}")
    body = "; ".join(f"{r['title']} ({r['employer_name']})" for r in jobs[:3])
    if len(jobs) > 3:
        body += f"; +{len(jobs) - 3} more"
    return f"CareerOS: {' and '.join(parts)}"[:TITLE_LIMIT], (body or "Open the review page to see them.")[:BODY_LIMIT]


def toast(title: str, body: str, runner=subprocess.run) -> bool:
    from careeros.demo.sandbox import enabled as demo_mode
    if sys.platform != "win32" or demo_mode():  # Demo Mode never starts another program
        return False
    env = {**os.environ, "CAREEROS_TOAST_TITLE": title, "CAREEROS_TOAST_BODY": body, "CAREEROS_TOAST_APP": APP_ID}
    try:
        result = runner(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
                        env=env, capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("notification failed: %s", exc)
        return False
    if result.returncode != 0:
        log.warning("notification failed: %s", (result.stderr or b"")[:300])
        return False
    return True
