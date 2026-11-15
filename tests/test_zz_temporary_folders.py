"""No temporary folder left behind (item 10 of the lead-resolution batch, 2026-11-02).

The suite and the tools used to leave thousands of careeros-* folders in the temporary folder (about 1 GB per full
planted-defect run): shutil.rmtree(..., ignore_errors=True) gives up silently on Windows on a read-only file (git's
objects) or a file still open (an SQLite database). They are now removed with careeros.core.scratch.remove_tree, which
raises instead of giving up.

This module's name sorts last, so `unittest discover` runs it after every other module, when every test's and every
class's cleanup has run. tests/__init__.py notes each careeros-* folder made in this process; any that still exists is a
leak (reported, and removed so that a failing run leaves nothing either). Run alone, nothing was made, and the check
passes. Only this process's own folders count: another run on the machine (a planted-defect run, say) never makes it
fail.
"""
import os
import stat
import tempfile
import unittest
from pathlib import Path

import tests
from careeros.core.scratch import remove_tree


class TemporaryFoldersTest(unittest.TestCase):
    def test_remove_tree_removes_read_only_files_and_says_when_it_cannot(self):
        folder = Path(tempfile.mkdtemp(prefix="careeros-scratch-test-"))
        try:
            (folder / "objects").mkdir()
            locked = folder / "objects" / "pack"
            locked.write_bytes(b"git object")
            os.chmod(locked, stat.S_IREAD)   # git writes its objects read-only
            remove_tree(folder)
            self.assertFalse(folder.exists())
            remove_tree(folder)   # already gone: nothing to do
        finally:
            if folder.exists():
                os.chmod(folder / "objects" / "pack", stat.S_IWRITE)
                remove_tree(folder)

    def test_each_careeros_temporary_folder_made_is_noted(self):
        made = tempfile.mkdtemp(prefix="careeros-noted-test-")
        try:
            self.assertIn(made, tests.MADE)
            with tempfile.TemporaryDirectory(prefix="careeros-noted-test-") as other:
                self.assertIn(other, tests.MADE)
            unnoted = tempfile.mkdtemp(prefix="other-")
            remove_tree(unnoted)
            self.assertNotIn(unnoted, tests.MADE)
        finally:
            remove_tree(made)

    def test_no_careeros_temporary_folder_made_in_this_run_is_left_behind(self):
        left = sorted(path for path in tests.MADE if os.path.exists(path))
        for path in left:   # reported below, and removed here so that even a failing run leaves nothing behind
            try:
                remove_tree(path)
            except OSError:
                pass
        self.assertEqual(left, [], "temporary folders left behind; remove them with careeros.core.scratch.remove_tree "
                                   "in a cleanup (addCleanup / addClassCleanup) or a finally")


if __name__ == "__main__":
    unittest.main()
