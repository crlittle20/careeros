"""`python -m careeros`: how the scheduled task starts CareerOS (pythonw, no window).

With `run --output-file FILE`, stdout and stderr point at the file before the engine is imported (OPP-52, 2026-11-01):
a broken install, a failed import or a bad vault path leaves its traceback in daily-run.log instead of nowhere.
Without it, nothing changes.
"""
import sys

from careeros.ops import daily_log   # the standard library only: it loads when the engine does not

log_file = daily_log.requested(sys.argv[1:])
if log_file:
    daily_log.redirect(log_file)
try:
    from careeros.cli import main
except BaseException:
    daily_log.load_failed()
    raise

main()
