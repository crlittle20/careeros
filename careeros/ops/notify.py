"""Windows notification after the daily run, so new jobs worth a look do not wait for you to open the report, and a
notice when something went wrong (`problem`), so a failure does not wait for you either.

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
BACKLOG_NOTICE_DAYS = 7   # OPP-01: postings unlabelled this long are named in the notification, even on a quiet day


def pursued_closures(store, since: str) -> int:
    """Postings you chose to pursue that closed since the last run (OPP-36, 2026-11-01): the latest PRIMARY label is Apply
    or Stretch, not dismissed (the latest dismissal event decides: a reinstated posting counts again), no application
    anywhere in the role group (a void one, recorded by mistake, is none), and closed after the previous completed run ended (with none, after `since`, this run's
    start). A failed run sends no summary, so its closures wait for the next completed one."""
    from careeros.review.applications import COUNTED, role_group
    from careeros.review.dismiss import dismissed_sql
    previous = store.one("SELECT MAX(ended_at) t FROM runs WHERE status = 'completed' AND ended_at < ?", (since,))["t"]
    rows = store.all(f"""SELECT j.job_id FROM jobs j WHERE j.current_state = 'closed' AND j.closed_at > ?
                          AND (SELECT label FROM labels l WHERE l.job_id = j.job_id AND l.profile = 'primary'
                               ORDER BY at DESC, rowid DESC LIMIT 1) IN ('apply','stretch')
                          AND NOT {dismissed_sql('j.job_id')}""", (previous or since,))
    count = 0
    for r in rows:
        group = role_group(store, r["job_id"])
        marks = ",".join("?" * len(group))
        if not store.one(f"SELECT 1 FROM applications WHERE job_id IN ({marks}) AND {COUNTED} LIMIT 1", tuple(group)):
            count += 1
    return count


def summary(store, since: str) -> tuple[str, str] | None:
    """(title, text) for jobs and email leads found since `since`, or None when there is nothing worth a ping. What needs
    you (pursued postings that closed since the last run; qualified postings unlabelled for BACKLOG_NOTICE_DAYS or more)
    leads the text, or is the title on a day with nothing new. Counts only: no posting is named for it."""
    from careeros.review.labels import label_backlog
    closed = pursued_closures(store, since)
    notes = [f"{closed} pursued posting{'s' if closed != 1 else ''} closed since the last run"] if closed else []
    backlog = label_backlog(store)
    if backlog and backlog["oldest_days"] >= BACKLOG_NOTICE_DAYS:
        notes.append(backlog["text"])
    jobs = store.all("""SELECT j.title, j.employer_name, s.tier FROM jobs j JOIN scores s ON s.score_id =
                        (SELECT score_id FROM scores WHERE job_id = j.job_id ORDER BY at DESC, rowid DESC LIMIT 1)
                        WHERE j.current_state = 'scored' AND s.tier IN ('A','B','near_miss') AND j.first_seen_at >= ?
                          AND j.variant_of IS NULL ORDER BY s.apply_priority DESC""", (since,))
    leads = store.one("SELECT COUNT(*) c FROM leads WHERE status = 'new' AND family IS NOT NULL AND first_seen_at >= ?", (since,))["c"]
    night = store.one("""SELECT COUNT(DISTINCT a.job_id) c FROM night_results a JOIN jobs j ON j.job_id = a.job_id
                         WHERE a.at >= ? AND j.first_seen_at >= ? AND a.tier IN ('A','B','near_miss')""", (since, since))["c"]
    if not jobs and not leads and not night and not notes:
        return None
    parts = []
    if jobs:
        parts.append(f"{len(jobs)} new job{'s' if len(jobs) != 1 else ''} worth a look")
    if night:
        parts.append(f"{night} NIGHT job{'s' if night != 1 else ''}")
    if leads:
        parts.append(f"{leads} new email lead{'s' if leads != 1 else ''}")
    listed = "; ".join(f"{r['title']} ({r['employer_name']})" for r in jobs[:3])
    if len(jobs) > 3:
        listed += f"; +{len(jobs) - 3} more"
    title = " and ".join(parts) if parts else notes.pop(0)
    body = ". ".join(notes + ([listed] if listed else [])) or "Open the review page to see them."
    return f"CareerOS: {title}"[:TITLE_LIMIT], body[:BODY_LIMIT]


def problem(title: str, body: str, runner=subprocess.run) -> bool:
    """A notice that something went wrong (OPP-54, 2026-11-01): a failed backup, a job-board provider turning degraded,
    a daily run held by a stale lock, a run where every board failed. Shown the same way as the summary (`toast`), within
    the same limits. Callers check operations.notifications, log the failure themselves, and name no posting, employer,
    pay or link."""
    return toast(title[:TITLE_LIMIT], body[:BODY_LIMIT], runner=runner)


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
