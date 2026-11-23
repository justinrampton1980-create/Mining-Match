"""Runs deploy/backup.sh for real against a temporary database and uploads folder.

Needs bash and the sqlite3, age, gzip, tar and flock command-line tools (DEPLOYMENT.md installs
them on the server); skipped where they aren't installed.
"""
import fcntl
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "backup.sh"
TOOLS = all(shutil.which(t) for t in ("bash", "sqlite3", "age", "age-keygen", "gzip", "tar", "flock"))
SECRET = b"Jordan-Private-7731"  # appears only in the source data; must never appear unencrypted elsewhere


@unittest.skipUnless(TOOLS, "needs bash, sqlite3, age, gzip, tar and flock")
class Backup(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.data = self.dir / "data"
        self.files = self.data / "uploads"
        (self.files / "ab").mkdir(parents=True)
        (self.files / "ab" / "licence.pdf").write_bytes(b"%PDF-1.4 licence of " + SECRET)
        self.db = self.data / "mm.db"
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA journal_mode=WAL")  # as the site runs it
        conn.execute("CREATE TABLE candidates (id INTEGER PRIMARY KEY, name TEXT, cv BLOB)")
        conn.execute("CREATE INDEX idx_name ON candidates (name)")
        conn.executemany("INSERT INTO candidates (name, cv) VALUES (?, ?)",
                         [(SECRET.decode(), os.urandom(3000)), ("Sam O'Neil", b"\x00\x01binary")])
        conn.commit()
        conn.close()
        self.key = self.dir / "key.txt"
        subprocess.run(["age-keygen", "-o", str(self.key)], check=True, capture_output=True)
        self.recipient = self.public_key(self.key)
        self.secret_key = next(l for l in self.key.read_text().splitlines() if l.startswith("AGE-SECRET-KEY-"))
        self.dest = self.dir / "backups"
        self.tmp = self.dir / "tmp"  # TMPDIR for the script: must stay empty
        self.tmp.mkdir()

    def tearDown(self):
        shutil.rmtree(self.dir)

    @staticmethod
    def public_key(key_file):
        return subprocess.run(["age-keygen", "-y", str(key_file)], check=True,
                              capture_output=True, text=True).stdout.strip()

    def run_backup(self, *args, recipient=None, path=None, alert=True, extra=""):
        env_file = self.dir / "mining-match.env"
        env_file.write_text(
            f'MM_DB="{self.db}"\nMM_FILES="{self.files}"\n'
            f'MM_BACKUP_RECIPIENT="{self.recipient if recipient is None else recipient}"\n'
            + ('MM_BACKUP_ALERT_EMAIL="owner@example.com"\n' if alert else "") + extra)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("MM_", "SMTP"))}
        env.update(MM_ENV_FILE=str(env_file), MM_BACKUP_DIR=str(self.dest), TMPDIR=str(self.tmp),
                   MM_PYTHON=sys.executable, MM_APP_DIR=str(ROOT))
        if path:
            env["PATH"] = path
        return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)

    def backups(self):
        return sorted(p.name for p in self.dest.iterdir()) if self.dest.exists() else []

    def decrypt(self, path: Path) -> bytes:
        return subprocess.run(["age", "-d", "-i", str(self.key), str(path)], check=True,
                              capture_output=True).stdout

    def assert_no_plaintext_anywhere(self):
        """Nothing outside the source data may contain the secret unencrypted."""
        for p in self.dir.rglob("*"):
            if p.is_file() and self.data not in p.parents:
                self.assertNotIn(SECRET, p.read_bytes(), p)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def assert_failed(self, r, reason_pattern):
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stderr, "FAILED: .*" + reason_pattern)
        self.assertIn("No backup was written", r.stderr)
        self.assertIn("Mining Match backup FAILED", r.stdout)  # the alert email (printed without SMTP)
        self.assertRegex(r.stdout, reason_pattern)
        self.assertEqual([b for b in self.backups() if not b.startswith("old-")], [])
        self.assert_no_plaintext_anywhere()

    # ------------------------------------------------------------ working backups

    def test_backups_are_encrypted_and_restore_exactly(self):
        r = self.run_backup()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("done:", r.stderr)
        made = self.backups()
        self.assertEqual(len(made), 2, made)
        self.assertRegex(made[0], r"^db-\d{4}-\d{2}-\d{2}\.sql\.gz\.age$")
        self.assertRegex(made[1], r"^uploads-\d{4}-\d{2}-\d{2}\.tar\.gz\.age$")
        self.assertEqual(self.dest.stat().st_mode & 0o777, 0o700)
        for name in made:
            f = self.dest / name
            self.assertTrue(f.read_bytes().startswith(b"age-encryption.org/v1\n"))
            self.assertEqual(f.stat().st_mode & 0o077, 0)
        self.assert_no_plaintext_anywhere()

        # Restore exactly as docs/DEPLOYMENT.md says.
        restored = self.dir / "restored.db"
        subprocess.run(f'age -d -i "{self.key}" "{self.dest / made[0]}" | gunzip | sqlite3 "{restored}"',
                       shell=True, check=True, executable="/bin/bash")
        src, dst = sqlite3.connect(self.db), sqlite3.connect(restored)
        try:
            q = "SELECT id, name, cv FROM candidates ORDER BY id"
            self.assertEqual(dst.execute(q).fetchall(), src.execute(q).fetchall())
            self.assertIn(("idx_name",), dst.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall())
        finally:
            src.close()
            dst.close()
        tar_path = self.dir / "out" / "uploads.tar.gz"
        tar_path.parent.mkdir()
        tar_path.write_bytes(self.decrypt(self.dest / made[1]))
        with tarfile.open(tar_path) as tar:
            self.assertEqual(tar.extractfile("uploads/ab/licence.pdf").read(), b"%PDF-1.4 licence of " + SECRET)

    def test_only_the_matching_private_key_opens_them(self):
        self.assertEqual(self.run_backup().returncode, 0)
        other = self.dir / "other-key.txt"
        subprocess.run(["age-keygen", "-o", str(other)], check=True, capture_output=True)
        r = subprocess.run(["age", "-d", "-i", str(other), str(next(self.dest.glob("db-*")))], capture_output=True)
        self.assertNotEqual(r.returncode, 0)

    def test_check_mode_validates_and_writes_nothing(self):
        r = self.run_backup("--check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("settings OK", r.stderr)
        self.assertEqual(self.backups(), [])

    def test_old_backups_are_pruned_and_recent_ones_kept(self):
        self.dest.mkdir(mode=0o700)
        old, recent = self.dest / "db-2020-01-01.sql.gz.age", self.dest / "db-2020-01-20.sql.gz.age"
        for f, days in ((old, 20), (recent, 10)):
            f.write_bytes(b"age-encryption.org/v1\n...")
            t = time.time() - days * 86400
            os.utime(f, (t, t))
        self.assertEqual(self.run_backup().returncode, 0)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_unencrypted_backups_from_older_versions_are_removed(self):
        self.dest.mkdir()
        (self.dest / "db-2026-11-01.sqlite").write_bytes(SECRET)
        (self.dest / "uploads-2026-11-01.tar.gz").write_bytes(SECRET)
        (self.dest / ".work.abc123").mkdir()
        (self.dest / ".work.abc123" / "db.sqlite").write_bytes(SECRET)
        (self.dest / "db-2026-11-01.sql.gz.age.tmp").write_bytes(b"half written")
        r = self.run_backup()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("removing old unencrypted backup db-2026-11-01.sqlite", r.stderr)
        self.assertTrue(all(b.endswith(".age") for b in self.backups()), self.backups())
        self.assert_no_plaintext_anywhere()

    # ------------------------------------------------------- never unencrypted

    def test_no_key_means_no_backup(self):
        self.assert_failed(self.run_backup(recipient=""), "MM_BACKUP_RECIPIENT isn't set")

    def test_the_private_key_by_mistake_is_refused(self):
        r = self.run_backup(recipient=self.secret_key)
        self.assert_failed(r, "PRIVATE key")
        self.assertNotIn(self.secret_key, r.stderr + r.stdout)  # never echoed into logs or email

    def test_a_malformed_key_is_refused(self):
        for bad in ("age1notakey", "ssh-ed25519 AAAAC3Nza", self.recipient[:-1] + "!", self.recipient + "x"):
            with self.subTest(bad=bad):
                self.assert_failed(self.run_backup(recipient=bad), "isn't an age public key")

    def test_a_missing_tool_means_no_backup(self):
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        for d in os.environ["PATH"].split(os.pathsep):
            if os.path.isdir(d):
                for name in os.listdir(d):
                    if name not in ("age", "age-keygen") and not (bin_dir / name).exists():
                        os.symlink(os.path.join(d, name), bin_dir / name)
        self.assert_failed(self.run_backup(path=str(bin_dir)), "the age tool isn't installed")

    def test_a_step_failing_part_way_keeps_nothing_from_that_run(self):
        # tar writes some output and then fails, after the database backup has been encrypted.
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "tar"
        fake.write_text("#!/bin/sh\nprintf 'partial'\nexit 2\n")
        fake.chmod(0o755)
        self.dest.mkdir(mode=0o700)
        keep = self.dest / "old-db-2026-11-01.sql.gz.age"  # an earlier good backup survives
        keep.write_bytes(b"age-encryption.org/v1\n...")
        r = self.run_backup(path=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        self.assert_failed(r, "the uploads backup failed")
        self.assertEqual(self.backups(), [keep.name])  # neither of tonight's files, not even the database

    def test_a_damaged_database_is_not_backed_up(self):
        data = bytearray(self.db.read_bytes())
        data[100:4096] = os.urandom(3996)
        self.db.write_bytes(bytes(data))
        for f in self.data.glob("mm.db-*"):
            f.unlink()
        self.assert_failed(self.run_backup(), "database")

    def test_missing_database_or_uploads_folder(self):
        self.assert_failed(self.run_backup(extra=f'MM_DB="{self.dir}/nope.db"\n'), "MM_DB")
        self.assert_failed(self.run_backup(extra=f'MM_FILES="{self.dir}/nope"\n'), "MM_FILES")

    def test_backup_folder_inside_the_uploads_is_refused(self):
        self.dest = self.files / "backups"
        r = self.run_backup()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("inside the uploads folder", r.stderr)
        self.assertEqual(self.backups(), [])

    def test_unexpected_files_in_the_backup_folder_fail_loudly(self):
        self.dest.mkdir()
        (self.dest / "notes.txt").write_text("someone put this here")
        r = self.run_backup()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("notes.txt", r.stderr)
        self.assertIn("Mining Match backup FAILED", r.stdout)
        self.assertTrue((self.dest / "notes.txt").exists())  # unknown files are reported, not deleted

    def test_two_backups_at_once_are_refused(self):
        self.dest.mkdir()
        with open(f"{self.dest}.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assert_failed(self.run_backup(), "another backup is still running")

    def test_no_alert_address_still_fails_safely(self):
        r = self.run_backup(recipient="", alert=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Mining Match backup FAILED", r.stdout)
        self.assertEqual(self.backups(), [])

    def test_unknown_option_is_refused(self):
        self.assert_failed(self.run_backup("--plaintext"), "unknown option")


class Script(unittest.TestCase):
    def test_no_unencrypted_write_paths_in_the_script(self):
        text = SCRIPT.read_text()
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn(".backup", code)  # no plain database copy to disk
        self.assertNotRegex(code, r"tar -c\w*f (?!-)")  # tar only ever writes to the pipe
        self.assertEqual(len(re.findall(r"\| age -r \"\$RECIPIENT\" -o", code)), 2)


if __name__ == "__main__":
    unittest.main()
