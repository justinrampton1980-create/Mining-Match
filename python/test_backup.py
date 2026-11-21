"""Runs deploy/backup.sh for real against a temporary database and uploads folder.

Needs the sqlite3 and age command-line tools (installed on the server by DEPLOYMENT.md);
skipped when they aren't on this machine.
"""
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "backup.sh"
TOOLS = all(shutil.which(t) for t in ("sh", "sqlite3", "age", "age-keygen"))


@unittest.skipUnless(TOOLS, "needs the sqlite3 and age command-line tools")
class Backup(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = self.dir / "mm.db"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE candidates (name TEXT)")
            conn.execute("INSERT INTO candidates VALUES ('Jordan Private')")
        conn.close()
        self.files = self.dir / "files"
        (self.files / "ab").mkdir(parents=True)
        (self.files / "ab" / "licence.pdf").write_bytes(b"%PDF-1.4 Jordan's licence")
        self.key = self.dir / "key.txt"
        subprocess.run(["age-keygen", "-o", str(self.key)], check=True, capture_output=True)
        self.recipient = subprocess.run(["age-keygen", "-y", str(self.key)], check=True,
                                        capture_output=True, text=True).stdout.strip()
        self.dest = self.dir / "backups"

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_backup(self, recipient):
        env_file = self.dir / "mining-match.env"
        env_file.write_text(f'MM_DB="{self.db}"\nMM_FILES="{self.files}"\nMM_BACKUP_RECIPIENT="{recipient}"\n')
        env = {**os.environ, "MM_ENV_FILE": str(env_file), "MM_BACKUP_DIR": str(self.dest)}
        env.pop("MM_BACKUP_RECIPIENT", None)
        return subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)

    def decrypt(self, path: Path) -> bytes:
        return subprocess.run(["age", "-d", "-i", str(self.key), str(path)], check=True,
                              capture_output=True).stdout

    def test_backups_are_encrypted_and_restore(self):
        r = self.run_backup(self.recipient)
        self.assertEqual(r.returncode, 0, r.stderr)
        made = sorted(p.name for p in self.dest.iterdir())
        self.assertEqual(len(made), 2, made)  # no unencrypted copies or work folder left behind
        db_file = next(self.dest.glob("db-*.sqlite.age"))
        uploads_file = next(self.dest.glob("uploads-*.tar.gz.age"))
        for f in (db_file, uploads_file):
            self.assertNotIn(b"Jordan", f.read_bytes())  # nothing readable without the key
            self.assertEqual(f.stat().st_mode & 0o077, 0)  # owner-only

        restored = self.dir / "restored.db"
        restored.write_bytes(self.decrypt(db_file))
        with sqlite3.connect(restored) as conn:
            self.assertEqual(conn.execute("SELECT name FROM candidates").fetchall(), [("Jordan Private",)])
        conn.close()
        tar_path = self.dir / "uploads.tar.gz"
        tar_path.write_bytes(self.decrypt(uploads_file))
        with tarfile.open(tar_path) as tar:
            member = tar.extractfile("files/ab/licence.pdf")
            self.assertEqual(member.read(), b"%PDF-1.4 Jordan's licence")

    def test_refuses_to_run_without_a_key(self):
        r = self.run_backup("")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("MM_BACKUP_RECIPIENT", r.stderr)
        self.assertFalse(self.dest.exists() and any(self.dest.iterdir()))  # nothing written at all


if __name__ == "__main__":
    unittest.main()
