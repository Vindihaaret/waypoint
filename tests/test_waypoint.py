"""Kjør med: python3 -m unittest discover -s tests (fra prosjektmappen)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from waypoint import system  # noqa: E402
from waypoint.restore import _safe_name  # noqa: E402

DUMP = """[org/gnome/desktop/background]
picture-uri='file:///home/a/bg.jpg'
picture-options='zoom'

[org/gnome/shell]
favorite-apps=['org.gnome.Nautilus.desktop']

[org/gnome/shell/extensions/dash-to-dock]
dock-position='BOTTOM'

[org/gnome/TextEditor]
style-scheme='Adwaita-dark'
"""


class DconfTests(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(system.dconf_category("org/gnome/shell/extensions/x"), "extensions")
        self.assertEqual(system.dconf_category("org/gnome/shell"), "desktop")
        self.assertEqual(system.dconf_category("org/gnome/TextEditor"), "apps")

    def test_filter_drops_wallpaper_keys(self):
        text, count = system.filter_dconf(DUMP, {"desktop"})
        self.assertNotIn("picture-uri", text)
        self.assertIn("picture-options='zoom'", text)
        self.assertIn("favorite-apps", text)
        self.assertNotIn("dock-position", text)
        self.assertEqual(count, 2)

    def test_filter_only_extensions(self):
        text, count = system.filter_dconf(DUMP, {"extensions"})
        self.assertEqual(count, 1)
        self.assertTrue(text.startswith("[org/gnome/shell/extensions/dash-to-dock]"))


class HelperTests(unittest.TestCase):
    def test_strv_roundtrip(self):
        items = ["a@b", "it's"]
        self.assertEqual(system.parse_strv(system.format_strv(items)), items)
        self.assertEqual(system.parse_strv("@as []"), [])

    def test_uri(self):
        p = Path("/home/a/Bilder/fin bakgrunn.jpg")
        self.assertEqual(system.uri_to_path(system.path_to_uri(p)), p)

    def test_safe_name(self):
        self.assertTrue(_safe_name("home/.config/x"))
        self.assertFalse(_safe_name("home/../../etc/passwd"))
        self.assertFalse(_safe_name("/etc/passwd"))

    def test_noise(self):
        self.assertTrue(system._is_noise("libfoo1"))
        self.assertFalse(system._is_noise("libadwaita-devel"))
        self.assertFalse(system._is_noise("vlc"))


class RoundTripTests(unittest.TestCase):  # eksport til én fil
    """Lager en kopi av en liten falsk hjemmemappe og legger den tilbake i en annen."""

    def test_files_roundtrip(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst, \
                tempfile.TemporaryDirectory() as out:
            Path(src, ".config/app").mkdir(parents=True)
            Path(src, ".config/app/settings.ini").write_text("x=1")
            Path(src, ".config/app/cache").mkdir()
            Path(src, ".config/app/cache/big").write_text("skal ikke med")
            os.link(Path(src, ".config/app/settings.ini"), Path(src, ".config/app/hardlenke.ini"))  # funn 3
            Path(src, ".bashrc").write_text("alias ll='ls -l'")
            Path(dst, ".bashrc").write_text("gammel")
            code = f"""
import sys; sys.path.insert(0, {str(ROOT)!r})
from pathlib import Path
from waypoint import backup, restore
from waypoint.model import Section, Group, Item
mode = sys.argv[1]
if mode == "backup":
    g = Group("files-shell", "x", items=[
        Item(id=".bashrc", label="b", data={{"path": ".bashrc", "category": "shell"}}),
        Item(id=".config/app", label="a", data={{"path": ".config/app", "category": "apps"}})])
    backup.write_backup([Section("Filer", "", [g])], Path({out!r}) / "waypoint-t.tar.gz")
else:
    b = restore.BackupFile(Path({out!r}) / "waypoint-t.tar.gz")
    secs = b.sections()
    r = restore.Restorer(b, secs)
    for st in r.steps():
        print(st.id, st.func(lambda t: None))
"""
            env = {**os.environ, "HOME": src}
            subprocess.run([sys.executable, "-c", code, "backup"], env=env, check=True, capture_output=True)
            env["HOME"] = dst
            res = subprocess.run([sys.executable, "-c", code, "restore"], env=env, check=True,
                                 capture_output=True, text=True)
            self.assertIn("files (True", res.stdout)
            self.assertEqual(Path(dst, ".config/app/settings.ini").read_text(), "x=1")
            self.assertEqual(Path(dst, ".config/app/hardlenke.ini").read_text(), "x=1")
            self.assertFalse(Path(dst, ".config/app/cache").exists())
            self.assertEqual(Path(dst, ".bashrc").read_text(), "alias ll='ls -l'")
            moved = list(Path(dst, ".local/share/waypoint/replaced").glob("*/.bashrc"))
            self.assertEqual(len(moved), 1)
            self.assertEqual(moved[0].read_text(), "gammel")


CRYPTO_SCRIPT = """
import sys, os; sys.path.insert(0, {root!r})
from pathlib import Path
from waypoint import backup, restore, crypto
from waypoint.model import Section, Group, Item
mode, out, enc_all, pw = sys.argv[1], Path(sys.argv[2]), sys.argv[3] == "1", sys.argv[4]
name = out / ("waypoint-t.tar.gz.gpg" if enc_all else "waypoint-t.tar.gz")
if mode == "backup":
    g = Group("files-x", "x", items=[
        Item(id=".bashrc", label="b", data={{"path": ".bashrc", "category": "shell"}}),
        Item(id=".ssh", label="s", sensitive=True, data={{"path": ".ssh", "category": "private"}})])
    m = backup.write_backup([Section("F", "", [g])], name, password=pw, encrypt_all=enc_all)
    print("ENC", m["encrypted"], "PRIV", bool(m.get("private")))
else:
    try:
        b = restore.BackupFile(name, None if pw == "-" else pw)
    except restore.NeedPassword:
        print("NEEDPW"); sys.exit(0)
    except crypto.BadPassword:
        print("BADPW"); sys.exit(0)
    secs = b.sections()
    r = restore.Restorer(b, secs)
    print("LOCKED", r.needs_private_password())
    if r.needs_private_password():
        print("UNLOCK", b.unlock(sys.argv[5]))
    for st in r.steps():
        print(st.id, st.func(lambda t: None))
"""


class CryptoTests(unittest.TestCase):
    """Private data skal aldri ligge ukryptert i arkivet, og må kunne gjenopprettes med passordet."""

    def _run(self, enc_all):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst, \
                tempfile.TemporaryDirectory() as out:
            Path(src, ".ssh").mkdir()
            Path(src, ".ssh/id_ed25519").write_text("HEMMELIG-NOKKEL")
            Path(src, ".bashrc").write_text("alias x=y")
            code = CRYPTO_SCRIPT.format(root=str(ROOT))
            env = {**os.environ, "HOME": src, "XDG_CONFIG_HOME": src + "/.config", "XDG_STATE_HOME": src + "/.state"}
            r = subprocess.run([sys.executable, "-c", code, "backup", out, "1" if enc_all else "0", "passord123"],
                               env=env, capture_output=True, text=True, check=True)
            self.assertIn("ENC all" if enc_all else "ENC None", r.stdout)
            archive = next(Path(out).iterdir())
            raw = archive.read_bytes()
            self.assertNotIn(b"HEMMELIG", raw)
            if not enc_all:
                import gzip
                self.assertNotIn(b"HEMMELIG", gzip.decompress(raw))
                self.assertIn(b"alias x=y", gzip.decompress(raw))
            env.update(HOME=dst, XDG_CONFIG_HOME=dst + "/.config", XDG_STATE_HOME=dst + "/.state")
            if enc_all:
                r = subprocess.run([sys.executable, "-c", code, "restore", out, "1", "-"], env=env,
                                   capture_output=True, text=True, check=True)
                self.assertIn("NEEDPW", r.stdout)
                r = subprocess.run([sys.executable, "-c", code, "restore", out, "1", "feil-passord"], env=env,
                                   capture_output=True, text=True, check=True)
                self.assertIn("BADPW", r.stdout)
                args = ["restore", out, "1", "passord123"]
            else:
                args = ["restore", out, "0", "-", "passord123"]
            r = subprocess.run([sys.executable, "-c", code, *args], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("files (True", r.stdout)
            self.assertEqual(Path(dst, ".ssh/id_ed25519").read_text(), "HEMMELIG-NOKKEL")
            self.assertEqual(Path(dst, ".bashrc").read_text(), "alias x=y")

    def test_private_only(self):
        self._run(False)

    def test_encrypt_all(self):
        self._run(True)


TAMPER_SCRIPT = """
import sys; sys.path.insert(0, {root!r})
from pathlib import Path
from waypoint import backup, restore
from waypoint.model import Section, Group, Item
mode, name = sys.argv[1], Path(sys.argv[2])
if mode == "backup":
    g = Group("files-x", "x", items=[
        Item(id=".bashrc", label="b", data={{"path": ".bashrc", "category": "shell"}}),
        Item(id="Stor", label="s", data={{"path": "Stor", "category": "personal"}})])
    backup.write_backup([Section("F", "", [g])], name, password="passord123", encrypt_all=True)
else:
    try:
        b = restore.BackupFile(name, "passord123")
    except restore.BadBackup as e:
        print("BADBACKUP", e); sys.exit(0)
    r = restore.Restorer(b, b.sections())
    for st in r.steps():
        print(st.id, st.func(lambda t: None))
"""


class TamperTests(unittest.TestCase):
    """H2: en kryptert kopi som er endret eller skadet, skal ikke gjenopprettes som om alt gikk bra."""

    def test_tampered_encrypted_backup_is_reported(self):
        if not shutil.which("gpg"):
            self.skipTest("gpg mangler")
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst, \
                tempfile.TemporaryDirectory() as out:
            Path(src, ".bashrc").write_text("alias x=y")
            Path(src, "Stor").mkdir()
            Path(src, "Stor/data.bin").write_bytes(os.urandom(6 * 1024 * 1024))  # flere gpg-blokker
            code = TAMPER_SCRIPT.format(root=str(ROOT))
            name = Path(out, "waypoint-t.tar.gz.gpg")
            env = {**os.environ, "HOME": src, "XDG_CONFIG_HOME": src + "/.config", "XDG_STATE_HOME": src + "/.state"}
            subprocess.run([sys.executable, "-c", code, "backup", str(name)], env=env, check=True,
                           capture_output=True)
            env.update(HOME=dst, XDG_CONFIG_HOME=dst + "/.config", XDG_STATE_HOME=dst + "/.state")
            # Uendret kopi: går bra
            r = subprocess.run([sys.executable, "-c", code, "restore", str(name)], env=env, capture_output=True,
                               text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("files (True", r.stdout)
            # Endre noen byte nær slutten
            data = bytearray(name.read_bytes())
            for i in range(len(data) - 200, len(data) - 190):
                data[i] ^= 0xFF
            name.write_bytes(bytes(data))
            shutil.rmtree(Path(dst, "Stor"))
            r = subprocess.run([sys.executable, "-c", code, "restore", str(name)], env=env, capture_output=True,
                               text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("files (True", r.stdout)
            self.assertTrue("files (False" in r.stdout or "BADBACKUP" in r.stdout, r.stdout)


class PlaceTests(unittest.TestCase):
    """Filer flyttes på plass; det som lå der fra før, havner i angremappen."""

    def test_place_moves_old_to_undo(self):
        from waypoint.restore import place
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst, \
                tempfile.TemporaryDirectory() as undo:
            Path(src, "Dokumenter/sub").mkdir(parents=True)
            Path(src, "Dokumenter/sub/a.txt").write_text("ny")
            Path(src, "Dokumenter/b.txt").write_text("b")
            Path(dst, "Dokumenter/sub").mkdir(parents=True)
            Path(dst, "Dokumenter/sub/a.txt").write_text("gammel")
            Path(dst, "Dokumenter/c.txt").write_text("urørt")
            counts = place(Path(src), Path(dst), Path(undo))
            self.assertEqual(counts, {"files": 2, "moved": 1})
            self.assertEqual(Path(dst, "Dokumenter/sub/a.txt").read_text(), "ny")
            self.assertEqual(Path(undo, "Dokumenter/sub/a.txt").read_text(), "gammel")
            self.assertEqual(Path(dst, "Dokumenter/c.txt").read_text(), "urørt")


class UniqueNameTests(unittest.TestCase):
    """M3: en fil som hentes tilbake flere ganger, skal aldri overskrive noe."""

    def test_unique_name(self):
        from waypoint.restore import unique_name
        with tempfile.TemporaryDirectory() as d:
            arc = Path(d, "arkiv.tar.gz")
            arc.write_text("x")
            self.assertEqual(unique_name(arc, "2026-11-22").name, "arkiv (2026-11-22).tar.gz")
            Path(d, "arkiv (2026-11-22).tar.gz").write_text("y")
            self.assertEqual(unique_name(arc, "2026-11-22").name, "arkiv (2026-11-22 2).tar.gz")
            folder = Path(d, "min.mappe")
            folder.mkdir()
            self.assertEqual(unique_name(folder, "2026-11-22").name, "min.mappe (2026-11-22)")
            self.assertEqual(unique_name(Path(d, ".bashrc"), "2026-11-22").name, ".bashrc (2026-11-22)")

    def test_restore_single_twice(self):
        from waypoint import restore

        class FakeRepo:
            def restore(self, snap_id, target, paths):
                for p in paths:
                    f = Path(target) / p.lstrip("/")
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text("fra kopien")

        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": home + "/.st"}), \
                mock.patch.object(restore, "HOME", Path(home)):
            Path(home, "notat.txt").write_text("original")
            snap = {"id": "s1", "time": "2026-11-22T10:00:00", "tags": ["home=" + home]}
            first = restore.restore_single(FakeRepo(), snap, home + "/notat.txt")
            second = restore.restore_single(FakeRepo(), snap, home + "/notat.txt")
            self.assertEqual(first.name, "notat (2026-11-22).txt")
            self.assertEqual(second.name, "notat (2026-11-22 2).txt")
            self.assertEqual(Path(home, "notat.txt").read_text(), "original")
            self.assertEqual(first.read_text(), "fra kopien")
            self.assertEqual(second.read_text(), "fra kopien")


class ExportSelfTests(unittest.TestCase):
    """M4: en eksport til en mappe som selv tas med, skal ikke ta med seg selv eller tidligere eksporter."""

    def test_export_into_included_folder(self):
        with tempfile.TemporaryDirectory() as home:
            docs = Path(home, "Dokumenter")
            docs.mkdir()
            Path(docs, "brev.txt").write_text("hei")
            Path(docs, "waypoint-gammel.tar.gz").write_bytes(b"tidligere eksport")
            Path(docs, "waypoint-avbrutt.tar.gz.part").write_bytes(b"halvskrevet")
            Path(docs, ".waypoint-private-abc.gpg").write_bytes(b"rest")
            code = f"""
import sys, tarfile; sys.path.insert(0, {str(ROOT)!r})
from pathlib import Path
from waypoint import backup
from waypoint.model import Section, Group, Item
g = Group("files-personal", "x", items=[Item(id="Dokumenter", label="d",
          data={{"path": "Dokumenter", "category": "personal"}})])
dest = Path({str(docs)!r}) / "waypoint-ny.tar.gz"
backup.write_backup([Section("F", "", [g])], dest)
print("\\n".join(tarfile.open(dest).getnames()))
"""
            env = {**os.environ, "HOME": home, "XDG_CONFIG_HOME": home + "/.config", "XDG_STATE_HOME": home + "/.st"}
            r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            names = r.stdout.split()
            self.assertIn("home/Dokumenter/brev.txt", names)
            for bad in ("waypoint-ny.tar.gz.part", "waypoint-ny.tar.gz", "waypoint-gammel.tar.gz",
                        "waypoint-avbrutt.tar.gz.part", ".waypoint-private-abc.gpg"):
                self.assertNotIn("home/Dokumenter/" + bad, names)


class ExtractTests(unittest.TestCase):
    """L2/L3: hardlenker bare til det som er lagt tilbake nå, og originalen tilbake når utpakkingen feiler."""

    def _tar(self, members):
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            for info, data in members:
                t.addfile(info, io.BytesIO(data) if data is not None else None)
        buf.seek(0)
        return tarfile.open(fileobj=buf, mode="r|")

    def _info(self, name, data=b"", link=None):
        import tarfile
        info = tarfile.TarInfo(name)
        if link:
            info.type, info.linkname = tarfile.LNKTYPE, link
        else:
            info.size = len(data)
        return info

    def test_hardlink_only_to_restored(self):
        from waypoint import restore
        with tempfile.TemporaryDirectory() as home, mock.patch.object(restore, "HOME", Path(home)):
            Path(home, "hemmelig").write_text("lå her fra før")
            fake = mock.Mock(undo_dir=Path(home, "undo"))
            tar = self._tar([(self._info("home/ok.txt", b"ny"), b"ny"),
                             (self._info("home/lenke-ok", link="home/ok.txt"), None),
                             (self._info("home/lenke-ut", link="home/hemmelig"), None)])
            counts, logs = {"files": 0, "moved": 0}, []
            restore.Restorer._extract(fake, tar, ["ok.txt", "lenke-ok", "lenke-ut"], counts, logs.append)
            self.assertEqual(Path(home, "lenke-ok").read_text(), "ny")
            self.assertFalse(Path(home, "lenke-ut").exists())
            self.assertEqual(counts["files"], 2)

    def test_original_put_back_on_failure(self):
        import tarfile
        from waypoint import restore
        with tempfile.TemporaryDirectory() as home, mock.patch.object(restore, "HOME", Path(home)):
            Path(home, "fil.txt").write_text("original")
            fake = mock.Mock(undo_dir=Path(home, "undo"))
            tar = self._tar([(self._info("home/fil.txt", b"ny"), b"ny")])
            counts, logs = {"files": 0, "moved": 0}, []
            with mock.patch.object(tarfile.TarFile, "extract", side_effect=OSError("disk full")):
                restore.Restorer._extract(fake, tar, ["fil.txt"], counts, logs.append)
            self.assertEqual(Path(home, "fil.txt").read_text(), "original")
            self.assertEqual(counts, {"files": 0, "moved": 0})


class FlatpakRestoreTests(unittest.TestCase):
    """Flatpak for hele systemet installeres av root-hjelperen (Flatpak selv får ikke spurt om passordet fra et
    vindu), Flatpak for brukeren direkte, samlet per kilde med én og én som reserve."""

    def _run(self, fail_together=False, helper_ok=("org.a.A", "org.b.B")):
        from waypoint import restore
        from waypoint.model import Group, Item, Section
        apps = [Item(id=i, label=i, data={"id": i, "name": i, "origin": o, "installation": inst})
                for i, o, inst in (("org.a.A", "flathub", "system"), ("org.b.B", "flathub", "system"),
                                   ("org.c.C", "flathub", "user"), ("org.d.D", "flathub", "user"))]
        g = Group("flatpaks", "f", items=apps, data={"remotes": [
            {"name": "flathub", "url": "https://dl.flathub.org/repo/", "installation": "system"}]})
        calls, plans = [], []

        def stream(cmd, log):
            calls.append(cmd)
            if "pkexec" in cmd:
                plans.append(json.loads(Path(cmd[-1]).read_text()))
                return 0, [f"@@OK {i}" for i in helper_ok]
            together = len([c for c in cmd if c.startswith("org.")]) > 1
            return (1 if (fail_together and together) else 0), []

        fake = mock.Mock(s=[Section("A", "", [g])], _stream=stream)
        with mock.patch.object(restore.system, "flatpak_remotes", return_value=[]), \
                mock.patch.object(restore.system, "package_manager", return_value="zypper"):
            ok, text = restore.Restorer.do_flatpaks(fake, lambda t: None)
        return ok, text, calls, plans

    def test_system_via_helper_user_direct(self):
        ok, _text, calls, plans = self._run()
        self.assertTrue(ok)
        self.assertEqual(calls[0][0], "pkexec")
        self.assertEqual(plans[0]["flatpaks"], [{"remote": "flathub", "id": "org.a.A"},
                                                {"remote": "flathub", "id": "org.b.B"}])
        self.assertEqual(plans[0]["flatpak_remotes"],
                         [{"name": "flathub", "url": "https://dl.flathub.org/repo/flathub.flatpakrepo"}])
        self.assertEqual(calls[1], ["flatpak", "install", "-y", "--noninteractive", "--user", "flathub",
                                    "org.c.C", "org.d.D"])
        self.assertFalse(any(c[0] == "flatpak" and "--system" in c for c in calls))

    def test_failures_reported(self):
        ok, text, calls, _plans = self._run(fail_together=True, helper_ok=("org.a.A",))
        self.assertFalse(ok)
        self.assertIn("org.b.B", text)
        self.assertEqual(calls[2:], [["flatpak", "install", "-y", "--noninteractive", "--user", "flathub", "org.c.C"],
                                     ["flatpak", "install", "-y", "--noninteractive", "--user", "flathub", "org.d.D"]])


class HelperPlanTests(unittest.TestCase):
    """Root-hjelperens plan, med run() byttet ut (ingen root): pakker og Flatpak for hele systemet."""

    def _plan(self, plan, fail=()):
        sys.path.insert(0, str(ROOT / "waypoint"))
        import privileged
        ran, said = [], []

        def run(cmd):
            ran.append(cmd)
            return 1 if any(f in cmd for f in fail) else 0
        with mock.patch.object(privileged, "run", run), mock.patch.object(privileged, "say", said.append), \
                mock.patch("shutil.which", return_value="/usr/bin/flatpak"):
            privileged.run_plan(plan)
        return ran, said

    def test_packages(self):
        ran, said = self._plan({"manager": "zypper", "packages": ["htop", "--force"]})
        self.assertIn(["zypper", "--non-interactive", "install", "--auto-agree-with-licenses", "--", "htop"], ran)
        self.assertIn("@@OK htop", said)
        self.assertIn("@@FAIL --force", said)

    def test_flatpaks(self):
        ran, said = self._plan({"manager": None,
                                "flatpak_remotes": [{"name": "flathub",
                                                     "url": "https://dl.flathub.org/repo/flathub.flatpakrepo"},
                                                    {"name": "-x", "url": "https://evil"}],
                                "flatpaks": [{"remote": "flathub", "id": "org.a.A"},
                                             {"remote": "flathub", "id": "org.b.B"},
                                             {"remote": "flathub", "id": "--installation=x"}]},
                               fail=("org.b.B",))
        self.assertEqual(ran[0], ["flatpak", "remote-add", "--system", "--if-not-exists", "--", "flathub",
                                  "https://dl.flathub.org/repo/flathub.flatpakrepo"])
        self.assertEqual(len([r for r in ran if "remote-add" in r]), 1)        # «-x» avvist
        self.assertIn(["flatpak", "install", "--system", "-y", "--noninteractive", "--", "flathub",
                       "org.a.A", "org.b.B"], ran)
        self.assertEqual([s for s in said if s.startswith("@@")],
                         ["@@STEP Installerer Flatpak-apper", "@@FAIL --installation=x", "@@OK org.a.A",
                          "@@FAIL org.b.B"])


class HomePathTests(unittest.TestCase):
    """Gjenoppretting til en annen hjemmemappe: bokmerker og standardmapper skal peke på den nye."""

    def test_rewrite_home(self):
        from waypoint.restore import rewrite_home
        text = ("file:///home/ola/Nedlastinger\nfile:///home/ola/Mine%20filer Mine\n"
                "favorite='/home/ola/x'\npath='/home/ola'\nannen=/home/olav/y\n")
        out = rewrite_home(text, "/home/ola", "/home/kari")
        self.assertIn("file:///home/kari/Nedlastinger", out)
        self.assertIn("file:///home/kari/Mine%20filer Mine", out)
        self.assertIn("favorite='/home/kari/x'", out)
        self.assertIn("path='/home/kari'", out)
        self.assertIn("annen=/home/olav/y", out)              # ikke en annen bruker med lignende navn
        self.assertEqual(rewrite_home(text, "/home/ola", "/home/ola"), text)

    def test_fix_home_paths(self):
        from waypoint import restore
        with tempfile.TemporaryDirectory() as home, mock.patch.object(restore, "HOME", Path(home)):
            Path(home, ".config/gtk-3.0").mkdir(parents=True)
            Path(home, ".config/gtk-3.0/bookmarks").write_text("file:///home/ola/Prosjekter Prosjekter\n")
            Path(home, ".config/user-dirs.dirs").write_text(
                'XDG_DOWNLOAD_DIR="$HOME/Downloads"\nXDG_DOCUMENTS_DIR="/home/ola/Documents"\n'
                'XDG_DESKTOP_DIR="$HOME/"\nXDG_MUSIC_DIR="/etc"\n')
            restore.fix_home_paths("/home/ola")
            self.assertEqual(Path(home, ".config/gtk-3.0/bookmarks").read_text(),
                             f"file://{home}/Prosjekter Prosjekter\n")
            self.assertIn(f'XDG_DOCUMENTS_DIR="{home}/Documents"', Path(home, ".config/user-dirs.dirs").read_text())
            self.assertTrue(Path(home, "Downloads").is_dir())
            self.assertTrue(Path(home, "Documents").is_dir())


class SizeLimitTests(unittest.TestCase):
    """Maks størrelse: lageret behandles som fullt når grensen nås."""

    def _repo(self, d, snaps):
        """Falskt lager: hvert tidspunkt er en (glissen) fil på 1 GB i lagermappen."""
        for sid, _t, _j in snaps:
            with open(Path(d, sid), "wb") as f:
                f.truncate(10**9)

        class FakeRepo:
            def location(self, mount=True):
                return d

            def snapshots(self, only_host=False):
                return [{"id": sid, "time": t, "tags": ["waypoint", f"job={j}"]} for sid, t, j in snaps
                        if Path(d, sid).exists()]

            def forget_ids(self, ids, prune=True):
                for i in ids:
                    Path(d, i).unlink()
        return FakeRepo()

    def test_limit(self):
        from waypoint import auto
        self.assertIsNone(auto.size_limit({}))
        self.assertIsNone(auto.size_limit({"max_size_gb": None}))
        self.assertEqual(auto.size_limit({"max_size_gb": 1000}), 10**12)

    def test_enforce_deletes_oldest_own_until_under(self):
        from waypoint import auto
        snaps = [("a1", "2026-01-01", "a"), ("b1", "2026-01-02", "b"), ("a2", "2026-02-01", "a"),
                 ("a3", "2026-03-01", "a"), ("a4", "2026-04-01", "a")]
        with tempfile.TemporaryDirectory() as d, mock.patch.object(auto.settings, "deleted_job_ids", return_value=set()):
            repo = self._repo(d, snaps)
            size = auto.enforce_limit(repo, {"id": "a", "max_size_gb": 3}, lambda t: None)
            self.assertEqual(size, 3 * 10**9)
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["a3", "a4", "b1"])  # b1: annen oppgave
            # Med reserve for neste kopi slettes ett til, men aldri det nyeste
            size = auto.enforce_limit(repo, {"id": "a", "max_size_gb": 3}, lambda t: None, reserve=10**9)
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["a4", "b1"])
            auto.enforce_limit(repo, {"id": "a", "max_size_gb": 1}, lambda t: None)
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["a4", "b1"])  # mer kan ikke slettes

    def test_no_limit_deletes_nothing(self):
        from waypoint import auto
        with tempfile.TemporaryDirectory() as d:
            repo = self._repo(d, [("a1", "2026-01-01", "a"), ("a2", "2026-02-01", "a")])
            self.assertEqual(auto.enforce_limit(repo, {"id": "a"}, lambda t: None), 2 * 10**9)
            self.assertEqual(len(list(Path(d).iterdir())), 2)


class DebugLogTests(unittest.TestCase):
    """Feilsøkingsloggen: bare når den er slått på, aldri passord, gamle logger ryddes."""

    def _env(self, home):
        return mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": home + "/c", "XDG_STATE_HOME": home + "/s"})

    def test_redact(self):
        from waypoint import debuglog
        debuglog.add_secret("hemmelig-passord-123")
        text = debuglog.redact("RESTIC_PASSWORD hemmelig-passord-123 smb://ola:pw42@nas/backup password=abc")
        self.assertNotIn("hemmelig-passord-123", text)
        self.assertNotIn("pw42", text)
        self.assertNotIn("abc", text)
        self.assertIn("smb://ola:" + debuglog.MASK + "@nas/backup", text)

    def test_only_when_enabled(self):
        from waypoint import debuglog, settings
        with tempfile.TemporaryDirectory() as home, self._env(home):
            debuglog.setup("test")
            debuglog.refresh()
            debuglog.debug("skal ikke skrives")
            self.assertEqual(list(debuglog.log_dir().glob("waypoint-*.log")) if debuglog.log_dir().exists() else [],
                             [])
            settings.set("debug", True)
            debuglog.refresh()
            debuglog.add_secret("superhemmelig99")
            debuglog.command(["restic", "snapshots"], 0, "ok", "feil med superhemmelig99")
            debuglog.tee(lambda t: None)("melding fra en kopi")
            logs = list(debuglog.log_dir().glob("waypoint-*.log"))
            self.assertEqual(len(logs), 1)
            text = logs[0].read_text()
            self.assertIn("$ restic snapshots  → exit 0", text)
            self.assertIn("melding fra en kopi", text)
            self.assertNotIn("superhemmelig99", text)
            self.assertEqual(oct(debuglog.log_dir().stat().st_mode & 0o777), "0o700")
            settings.set("debug", False)
            debuglog.refresh()

    def test_cleanup_and_restore_log(self):
        import time
        from waypoint import debuglog
        with tempfile.TemporaryDirectory() as home, self._env(home):
            d = debuglog.log_dir()
            d.mkdir(parents=True)
            old, new = d / "waypoint-2020-01-01.log", d / "waypoint-ny.log"
            old.write_text("x")
            new.write_text("y")
            os.utime(old, (time.time() - 30 * 86400,) * 2)
            debuglog.cleanup()
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())
            rl = debuglog.RestoreLog()        # alltid, også uten feilsøkingsmodus
            debuglog.add_secret("passordet-mitt")
            rl.write("steg 1 med passordet-mitt")
            rl.close()
            text = rl.path.read_text()
            self.assertIn("steg 1 med", text)
            self.assertNotIn("passordet-mitt", text)
            self.assertEqual(oct(rl.path.stat().st_mode & 0o777), "0o600")


class OwnInstallTests(unittest.TestCase):
    """Waypoint sin egen installasjon legges aldri tilbake fra en kopi, og snarveier får ny hjemmemappe."""

    def test_own_install(self):
        from waypoint.restore import own_install
        for rel in (".local/bin/waypoint", ".local/share/waypoint/app/waypoint/app.py",
                    ".local/share/applications/io.github.waypoint.Waypoint.desktop"):
            self.assertTrue(own_install(rel), rel)
        for rel in (".local/bin/annet", ".local/share/applications/org.example.App.desktop",
                    ".local/share/waypoint/replaced/x", ".local/bin/waypoint-ekstra"):
            self.assertFalse(own_install(rel), rel)

    def test_place_skips_own_install(self):
        from waypoint.restore import own_install, place
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            for rel, text in ((".local/bin/waypoint", "fra kopien"), (".local/bin/annet", "annet"),
                              (".local/share/waypoint/app/x.py", "gammel kode")):
                Path(src, rel).parent.mkdir(parents=True, exist_ok=True)
                Path(src, rel).write_text(text)
            Path(dst, ".local/bin").mkdir(parents=True)
            Path(dst, ".local/bin/waypoint").write_text("installert her")
            place(Path(src), Path(dst), Path(dst, "undo"), skip=own_install)
            self.assertEqual(Path(dst, ".local/bin/waypoint").read_text(), "installert her")
            self.assertEqual(Path(dst, ".local/bin/annet").read_text(), "annet")
            self.assertFalse(Path(dst, ".local/share/waypoint/app").exists())

    def test_extract_skips_own_install(self):
        import io
        import tarfile
        from waypoint import restore
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            for name, data in (("home/.local/bin/waypoint", b"fra kopien"), ("home/.local/bin/annet", b"annet")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        buf.seek(0)
        with tempfile.TemporaryDirectory() as home, mock.patch.object(restore, "HOME", Path(home)):
            Path(home, ".local/bin").mkdir(parents=True)
            Path(home, ".local/bin/waypoint").write_text("installert her")
            fake = mock.Mock(undo_dir=Path(home, "undo"))
            restore.Restorer._extract(fake, tarfile.open(fileobj=buf, mode="r|"), [".local/bin"],
                                      {"files": 0, "moved": 0}, lambda t: None)
            self.assertEqual(Path(home, ".local/bin/waypoint").read_text(), "installert her")
            self.assertEqual(Path(home, ".local/bin/annet").read_text(), "annet")

    def test_desktop_files_get_new_home(self):
        from waypoint import restore
        with tempfile.TemporaryDirectory() as home, mock.patch.object(restore, "HOME", Path(home)):
            apps = Path(home, ".local/share/applications")
            apps.mkdir(parents=True)
            Path(apps, "egen.desktop").write_text("[Desktop Entry]\nExec=/home/ola/.local/bin/egen\n"
                                                  "Icon=/home/ola/.local/share/icons/egen.png\n")
            restore.fix_home_paths("/home/ola")
            text = Path(apps, "egen.desktop").read_text()
            self.assertIn(f"Exec={home}/.local/bin/egen", text)
            self.assertIn(f"Icon={home}/.local/share/icons/egen.png", text)


class OtherDistroTests(unittest.TestCase):
    """Gjenoppretting til en annen distribusjon (eller en annen versjon av samme): pakker som ikke finnes her, gråes
    ut eller velges ikke, pakker med samme navn kan velges, og et banner forklarer det."""

    def _sections(self, manager_here, found, installed=(), distro_here=None):
        from waypoint import restore
        manifest = {"app": "waypoint", "distro": {"id": "opensuse-tumbleweed", "name": "openSUSE Tumbleweed"},
                    "repos": {"manager": "zypper", "items": [
                        {"alias": "packman", "name": "Packman", "url": "https://ftp.gwdg.de/pub/linux/packman/"}]},
                    "packages": {"manager": "zypper", "items": [
                        {"name": "htop", "summary": "Prosesser"}, {"name": "vlc-codecs"},
                        {"name": "libxkbcommon-compat"}, {"name": "git"}]},
                    "flatpaks": {"items": [{"id": "org.localsend.localsend_app", "name": "LocalSend",
                                            "origin": "flathub", "installation": "system"}]}}
        here = distro_here or {"id": "fedora", "name": "Fedora 43"}
        fake = mock.Mock(manifest=manifest, encrypted_all=False, staging=Path("/finnes/ikke"))
        with mock.patch.object(restore.system, "package_manager", return_value=manager_here), \
                mock.patch.object(restore.system, "installed_packages", return_value=set(installed)), \
                mock.patch.object(restore.system, "available_packages", return_value=found), \
                mock.patch.object(restore.system, "os_release", return_value=here), \
                mock.patch.object(restore.system, "have", return_value=False), \
                mock.patch.object(restore.system, "flatpak_apps", return_value=[]), \
                mock.patch.object(restore.system, "repositories", return_value=[]):
            return restore.BackupFile.sections(fake)

    def _packages(self, *a, **kw):
        from waypoint.model import find_group
        return {i.id: i for i in find_group(self._sections(*a, **kw), "packages").items}

    def test_other_distribution_gives_a_list(self):
        from waypoint.model import find_group
        secs = self._sections("dnf", {"htop", "git"}, installed={"git"})
        self.assertIsNone(find_group(secs, "repos"))             # ingen kilder fra et annet system
        g = find_group(secs, "packages")
        self.assertFalse(any(i.available or i.selected for i in g.items))   # ingenting installeres
        self.assertEqual(g.data["copy"].split(), ["htop", "vlc-codecs", "libxkbcommon-compat"])
        items = {i.id: i for i in g.items}
        self.assertNotEqual(items["htop"].detail, items["vlc-codecs"].detail)  # samme navn finnes her: merket
        self.assertIn("openSUSE Tumbleweed", g.note)
        fp = find_group(secs, "flatpaks")
        self.assertTrue(fp.note)                                 # Flatpak mangler: beskjed, ikke valgbar
        self.assertFalse(any(i.available for i in fp.items))

    def test_same_package_manager_other_distribution(self):
        items = self._packages("zypper", {"htop"}, distro_here={"id": "opensuse-leap", "name": "openSUSE Leap"})
        self.assertFalse(any(i.available for i in items.values()))

    def test_same_manager_other_version(self):
        items = self._packages("zypper", {"htop", "git", "vlc-codecs"},
                               distro_here={"id": "opensuse-tumbleweed", "name": "openSUSE Tumbleweed"})
        self.assertTrue(items["htop"].selected)
        self.assertTrue(items["libxkbcommon-compat"].available)  # kan komme fra en pakkekilde i kopien
        self.assertFalse(items["libxkbcommon-compat"].selected)  # men velges ikke, så den ikke feiler

    def test_query_failed(self):
        items = self._packages("zypper", None, distro_here={"id": "opensuse-tumbleweed"})
        self.assertTrue(all(i.selected for i in items.values()))  # vet ikke: alt som før
        items = self._packages("dnf", None)                       # annen distribusjon: bare en liste
        self.assertFalse(any(i.available for i in items.values()))

    def test_other_system(self):
        from waypoint import restore
        with mock.patch.object(restore.system, "os_release", return_value={"id": "fedora"}), \
                mock.patch.object(restore.system, "package_manager", return_value="dnf"):
            self.assertTrue(restore.other_system({"distro": {"id": "ubuntu"}}))
            self.assertFalse(restore.other_system({"distro": {"id": "fedora"}, "packages": {"manager": "dnf"}}))
            self.assertTrue(restore.other_system({"packages": {"manager": "apt"}}))   # gammel kopi uten distro
            self.assertFalse(restore.other_system({}))

    def test_distro_mismatch(self):
        from waypoint import restore
        with mock.patch.object(restore.system, "os_release", return_value={"id": "fedora", "name": "Fedora 43"}):
            text = restore.distro_mismatch({"distro": {"id": "opensuse-tumbleweed", "name": "openSUSE Tumbleweed"}})
            self.assertIn("openSUSE Tumbleweed", text)
            self.assertIn("Fedora 43", text)
            self.assertIsNone(restore.distro_mismatch({"distro": {"id": "fedora", "name": "Fedora 42"}}))
            self.assertIsNone(restore.distro_mismatch({}))


class ResticPrepTests(unittest.TestCase):
    def test_exclude_patterns(self):
        from waypoint import backup
        home = str(Path.home())
        pats = backup.exclude_patterns(["node_modules", "~/Videoer/Opptak"], ["/mnt/disk [1]/Waypoint"])
        self.assertIn("node_modules", pats)
        self.assertIn(home + "/Videoer/Opptak", pats)
        self.assertIn("/mnt/disk \\[1\\]/Waypoint", pats)  # stier fra systemet er bokstavelige
        self.assertIn(home + "/.config/**/GPUCache", pats)
        # funn 6: navn som «Cache» og «lock» gjelder bare i programdata, aldri i personlige mapper
        for bare in ("Cache", "cache", "lock", "Crashpad", "cache2", "Logs"):
            self.assertNotIn(bare, pats)
        exports = [p for p in pats if "waypoint-" in p or p.startswith("flytt-")]
        self.assertIn("waypoint-*.tar.gz", exports)        # M4: eksportfiler tas ikke med
        self.assertTrue(all(p.startswith(home + "/.") for p in pats if p not in
                            ("node_modules", home + "/Videoer/Opptak", "/mnt/disk \\[1\\]/Waypoint", *exports)))

    def test_prepare_snapshot_paths(self):
        code = f"""
import sys, json; sys.path.insert(0, {str(ROOT)!r})
from pathlib import Path
from waypoint import backup
from waypoint.model import Section, Group, Item
home = Path.home()
g = Group("files-x", "x", items=[
    Item(id="Dokumenter", label="d", data={{"path": "Dokumenter", "category": "personal"}}),
    Item(id=".config/app", label="a", data={{"path": ".config/app", "category": "apps"}}),
    Item(id=".config/app/sub", label="s", data={{"path": ".config/app/sub", "category": "apps"}})])
paths, m = backup.prepare_snapshot([Section("F", "", [g])], home / "setup")
print(json.dumps({{"paths": paths, "files": len(m["files"]), "enc": m["encrypted"],
                  "manifest": (home / "setup/manifest.json").exists()}}))
"""
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, "-c", code], env={**os.environ, "HOME": home},
                               capture_output=True, text=True, check=True)
            out = __import__("json").loads(r.stdout)
            self.assertEqual(sorted(out["paths"]), sorted([f"{home}/setup", f"{home}/Dokumenter",
                                                          f"{home}/.config/app"]))
            self.assertEqual(out["enc"], "restic")
            self.assertTrue(out["manifest"])


RESTIC = shutil.which("restic") or os.environ.get("WAYPOINT_RESTIC")


@unittest.skipUnless(RESTIC, "restic er ikke installert")
class ResticRoundTripTests(unittest.TestCase):
    """Hele runden: sikkerhetskopi to ganger (bare endringer), list tidspunkter, gjenopprett og hent én fil."""

    def test_roundtrip(self):
        code = f"""
import sys, json, os; sys.path.insert(0, {str(ROOT)!r})
from pathlib import Path
from waypoint import auto, settings, repo, restore
home = Path.home()
mode = sys.argv[1]
job = settings.jobs()[0] if settings.jobs() else None
if mode == "init":
    repo.Repo.for_job(job).init()
elif mode == "backup":
    print(auto.run(force=True, log=lambda t: None))
    st = settings.job_status(job["id"]); print(st["state"], st.get("last_error"))
elif mode == "snaps":
    r = repo.Repo.for_job(job); print(len(r.snapshots(job_id=job["id"])))
elif mode == "restore":
    r = repo.Repo(json.loads(sys.argv[2]), "passord123"); snap = r.snapshots()[0]
    b = restore.SnapshotBackup(r, snap)
    secs = b.sections()
    rs = restore.Restorer(b, secs)
    for st in rs.steps():
        if st.id in ("files",):
            print(st.id, st.func(lambda t: None))
    print(json.dumps(restore.restore_single(r, r.snapshots()[-1], b.manifest["home"] + "/Dokumenter/brev.txt").name))
"""
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst, \
                tempfile.TemporaryDirectory() as store:
            for h in (src, dst):
                Path(h, ".config/waypoint").mkdir(parents=True)
                jobs = [{"id": "j1", "name": "Alt", "enabled": True, "interval": "daily", "preset": "all",
                         "choices": {}, "storage": {"kind": "local", "path": store}}] if h == src else []
                Path(h, ".config/waypoint/settings.json").write_text(__import__("json").dumps(
                    {"jobs": jobs, "tray": False, "skip_on_battery": False, "check_days": 0}))
                Path(h, ".config/waypoint/.migrated-from-flytt").touch()
            Path(src, "Dokumenter").mkdir()
            Path(src, "Dokumenter/brev.txt").write_text("versjon 1")
            # Funn 6: mapper som heter «Cache» eller «lock» i personlige mapper skal med …
            Path(src, "Dokumenter/Cache").mkdir()
            Path(src, "Dokumenter/Cache/notat.txt").write_text("viktig")
            Path(src, "Dokumenter/lock").write_text("også viktig")
            # … men mellomlager i programdata skal ikke.
            Path(src, ".config/app/Cache").mkdir(parents=True)
            Path(src, ".config/app/Cache/søppel").write_text("x")
            Path(src, ".config/app/settings.ini").write_text("a=1")
            # Funn 7: glob-tegn i navn
            Path(src, "Musikk/Album [2020]").mkdir(parents=True)
            Path(src, "Musikk/Album [2020]/spor*1?.txt").write_text("lyd")
            env = {**os.environ, "HOME": src, "XDG_CONFIG_HOME": src + "/.config", "XDG_STATE_HOME": src + "/.state",
                   "XDG_CACHE_HOME": src + "/.cache", "WAYPOINT_PASSWORD": "passord123"}
            run = lambda *a, e=env: subprocess.run([sys.executable, "-c", code, *a], env=e, capture_output=True,
                                                   text=True, timeout=600)
            # Funn 4: uten et opprettet lager skal den automatiske kopien vente, ikke lage et nytt lager.
            r = run("backup")
            self.assertIn("unavailable", r.stdout, r.stdout + r.stderr)
            self.assertFalse(Path(store, "Waypoint").exists())
            run("init")
            r = run("backup")
            self.assertIn("ok None", r.stdout, r.stdout + r.stderr)
            Path(src, "Dokumenter/brev.txt").write_text("versjon 2")
            r = run("backup")
            self.assertIn("ok None", r.stdout, r.stdout + r.stderr)
            self.assertIn("2", run("snaps").stdout)
            env2 = {**env, "HOME": dst, "XDG_CONFIG_HOME": dst + "/.config", "XDG_STATE_HOME": dst + "/.state",
                    "XDG_CACHE_HOME": dst + "/.cache"}
            Path(dst, "Dokumenter").mkdir()
            Path(dst, "Dokumenter/brev.txt").write_text("lokal")
            r = run("restore", __import__("json").dumps({"kind": "local", "path": store}), e=env2)
            self.assertIn("files (True", r.stdout, r.stdout + r.stderr)
            self.assertEqual(Path(dst, "Dokumenter/brev.txt").read_text(), "versjon 2")
            self.assertEqual(Path(dst, "Dokumenter/Cache/notat.txt").read_text(), "viktig")
            self.assertEqual(Path(dst, "Dokumenter/lock").read_text(), "også viktig")
            self.assertEqual(Path(dst, ".config/app/settings.ini").read_text(), "a=1")
            self.assertFalse(Path(dst, ".config/app/Cache").exists())
            self.assertEqual(Path(dst, "Musikk/Album [2020]/spor*1?.txt").read_text(), "lyd")
            undo = list(Path(dst, ".local/share/waypoint/replaced").glob("*/Dokumenter/brev.txt"))
            self.assertEqual(undo[0].read_text(), "lokal")
            # Den eldste versjonen hentet som egen fil ved siden av
            self.assertTrue(any("versjon 1" == p.read_text() for p in Path(dst, "Dokumenter").glob("brev*")))


class ReviewFixTests(unittest.TestCase):
    """Tester for funnene fra kodesjekken (REVIEW.md)."""

    def test_package_filter(self):  # funn 9
        noise = ["libavcodec61", "libgtk-4-1", "libnvidia-egl-gbm1-32bit", "libssl3t64", "rpm", "systemd",
                 "kernel-default", "grub2-x86_64-efi", "patterns-base-base"]
        keep = ["libreoffice", "libreoffice-writer", "libvirt", "librewolf", "libadwaita-devel", "rpm-build",
                "systemd-network", "kernel-default-devel", "vlc", "patterns-gnome-gnome", "libinput-tools"]
        for n in noise:
            self.assertTrue(system._is_noise(n), n)
        for n in keep:
            self.assertFalse(system._is_noise(n), n)

    def test_glob_escape(self):  # funn 7
        from waypoint.repo import glob_escape
        self.assertEqual(glob_escape("/a/Album [2020]/x*?"), "/a/Album \\[2020\\]/x\\*\\?")

    def test_space_retry_deletes_one_at_a_time(self):  # funn 2
        from waypoint import auto, repo

        class FakeRepo:
            remote = False

            def __init__(self):
                self.snaps = [{"id": str(i), "time": f"2026-01-{i + 1:02d}"} for i in range(6)]
                self.forgotten = []

            def snapshots(self, only_host=False, job_id=None):
                return list(self.snaps)

            def unlock(self):
                pass

            def forget_ids(self, ids, prune=True):
                self.forgotten += ids
                self.snaps = [s for s in self.snaps if s["id"] not in ids]

        fake = FakeRepo()
        calls = []

        def do():
            calls.append(1)
            if len(calls) < 3:  # full disk de to første gangene
                raise repo.RepoError("Fatal: unable to save snapshot",
                                     "write /x: no space left on device\nFatal: unable to save snapshot")
            return {"ok": True}
        self.assertEqual(auto._with_space_retry(fake, do, lambda t: None), {"ok": True})
        self.assertEqual(fake.forgotten, ["0", "1"])  # bare de to eldste, ikke «alt unntatt nyeste»

        fake2, never = FakeRepo(), lambda: (_ for _ in ()).throw(repo.RepoError("x", "no space left on device"))
        with self.assertRaises(repo.RepoError):
            auto._with_space_retry(fake2, never, lambda t: None)
        self.assertEqual(len(fake2.snaps), 6 - auto.SPACE_RETRIES)
        self.assertGreaterEqual(len(fake2.snaps), 1)

    def _isolated(self, code: str) -> str:
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home, "XDG_CONFIG_HOME": home + "/.config", "XDG_STATE_HOME": home + "/.st"}
            r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r})\n" + code],
                               env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout

    def test_status_concurrent_writers(self):  # funn 8
        out = self._isolated("""
import multiprocessing, json
from waypoint import settings
def worker(n):
    for i in range(150):
        settings.update_status(**{f"w{n}": i})
if __name__ == "__main__":
    ps = [multiprocessing.Process(target=worker, args=(n,)) for n in range(4)]
    [p.start() for p in ps]; [p.join() for p in ps]
    s = settings.read_status()
    print(all(p.exitcode == 0 for p in ps), [s.get(f"w{n}") for n in range(4)])
""")
        self.assertIn("True [149, 149, 149, 149]", out)

    def test_stale_running_is_shown_as_interrupted(self):  # mindre funn: siden står ikke på «pågår»
        out = self._isolated("""
from waypoint import settings
settings.save_job({"id": "j1", "name": "x"})
settings.update_job_status("j1", state="running", pid=999999999)
print(settings.job_status("j1")["state"])
settings.update_job_status("j1", checking=True, check_pid=999999999)
print(settings.job_status("j1")["checking"])
""")
        self.assertEqual(out.split(), ["error", "False"])

    def test_notify_once(self):  # funn 10
        out = self._isolated("""
from waypoint import auto
sent = []
auto.notify = lambda t, b: sent.append(t)
for _ in range(5):
    auto.notify_once("k", "a", "b")
auto.notify_once("k2", "c", "d")
print(sent)
""")
        self.assertIn("['a', 'c']", out)

    def test_privileged_validation(self):  # mindre funn: ingen opsjoner fra manifestet til root
        sys.path.insert(0, str(ROOT / "waypoint"))
        import privileged
        self.assertTrue(privileged.PACKAGE.fullmatch("libstdc++6"))
        for bad in ("--force", "-x", "a b", "pkg;rm"):
            self.assertIsNone(privileged.PACKAGE.fullmatch(bad), bad)
        self.assertIsNone(privileged.URL.fullmatch("--root=/"))
        self.assertTrue(privileged.ALIAS.fullmatch("NVIDIA:repo-non-free"))

    def test_valid_dnf_repo(self):  # H3: bare én signert kilde med kjente nøkler skrives som root
        sys.path.insert(0, str(ROOT / "waypoint"))
        import privileged
        two = ("[a]\nname=A\nbaseurl=https://a.example/repo\n"
               "[b]\nname=B\nbaseurl=https://b.example/repo\n")
        nocheck = "[a]\nname=A\nbaseurl=https://a.example/repo\ngpgcheck=0\n"
        norepocheck = "[a]\nname=A\nmetalink=https://a.example/ml\nrepo_gpgcheck=false\n"
        nourl = "[a]\nname=A\ngpgcheck=1\n"
        badurl = "[a]\nname=A\nbaseurl=file:///tmp/evil\n"
        good = ("[copr:copr.example:u:p]\nname=Copr\nbaseurl=https://c.example/$releasever/\n"
                "  https://mirror.example/$basearch/\ngpgcheck=1\ngpgkey=https://c.example/key.gpg\n"
                "enabled=1\nsslverify=0\nexclude=kernel*\n")
        for bad in (two, nocheck, norepocheck, nourl, badurl, "ikke en ini-fil", None):
            self.assertIsNone(privileged.valid_dnf_repo(bad), bad)
        clean = privileged.valid_dnf_repo(good)
        self.assertEqual(clean, "[copr:copr.example:u:p]\nname=Copr\n"
                                "baseurl=https://c.example/$releasever/ https://mirror.example/$basearch/\n"
                                "gpgkey=https://c.example/key.gpg\nenabled=1\ngpgcheck=1\n")
        self.assertNotIn("sslverify", clean)  # ukjente nøkler droppes


class HelperTests2(unittest.TestCase):
    """M1: pakker installeres av den root-eide hjelperen. Mangler den, legger appens kopi den inn selv (ett
    passordvindu), og etterpå kjøres aldri en fil fra hjemmemappen som root."""

    def test_helper_ready_and_command(self):
        from waypoint import restore
        from waypoint.privileged import HELPER_VERSION
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d, "waypoint-privileged")
            with mock.patch.dict(os.environ, {"WAYPOINT_HELPER": str(fake)}):
                self.assertFalse(restore.helper_ready())                                 # mangler
                self.assertEqual(restore.helper_command("/tmp/plan.json"),
                                 ["pkexec", "/usr/bin/python3", "-I", str(restore.PRIVILEGED), "--install-and-run",
                                  "/tmp/plan.json"])
                fake.write_text(f"HELPER_VERSION = {HELPER_VERSION}\n")
                self.assertEqual(restore.helper_version(fake), HELPER_VERSION)
                self.assertFalse(restore.helper_ready())                                 # eid av brukeren
                with mock.patch.object(Path, "stat", return_value=mock.Mock(st_uid=0)):
                    self.assertTrue(restore.helper_ready())                              # root-eid og oppdatert
                    self.assertEqual(restore.helper_command("/tmp/p.json"), ["pkexec", str(fake), "/tmp/p.json"])
                    fake.write_text(f"HELPER_VERSION = {HELPER_VERSION - 1}\n")
                    self.assertFalse(restore.helper_ready())                             # for gammel

    def test_install_restic_installs_helper_first(self):
        from waypoint import repo
        with mock.patch.dict(os.environ, {"WAYPOINT_HELPER": "/finnes/ikke"}), \
                mock.patch.object(repo.system, "package_manager", return_value="zypper"), \
                mock.patch.object(subprocess, "run", return_value=mock.Mock(stdout="")) as run:
            repo.install_restic(log=lambda t: None)
            self.assertIn("--install-and-run", run.call_args[0][0])

    def test_self_install(self):
        sys.path.insert(0, str(ROOT / "waypoint"))
        import privileged
        with tempfile.TemporaryDirectory() as d:
            helper, policy = Path(d, "libexec/waypoint/waypoint-privileged"), Path(d, "actions/x.policy")
            with mock.patch.object(privileged, "HELPER_PATH", str(helper)), \
                    mock.patch.object(privileged, "POLICY_PATH", str(policy)), \
                    mock.patch.object(privileged.os, "chown"), mock.patch.object(privileged, "say"):
                self.assertEqual(privileged.install_self(), 0)
            self.assertEqual(helper.read_bytes(), (ROOT / "waypoint/privileged.py").read_bytes())
            self.assertEqual(oct(helper.stat().st_mode & 0o777), "0o755")
            self.assertIn("org.freedesktop.policykit.exec.path", policy.read_text())
            self.assertIn(privileged.HELPER_PATH, privileged.POLICY)


class NetworkTests(unittest.TestCase):
    """NAS: serveradresser, visning og mappevalg (selve innloggingen krever en ekte NAS)."""

    def test_server_and_uris(self):
        from waypoint import network as n
        self.assertEqual(n.clean_server("  smb://ola@NAS.local/backup/x "), "NAS.local")
        self.assertEqual(n.clean_server("192.168.1.10/"), "192.168.1.10")
        self.assertEqual(n.server_uri("smb", "nas"), "smb://nas/")
        self.assertEqual(n.server_uri("sftp", "nas", "ola nordmann"), "sftp://ola%20nordmann@nas/")
        self.assertEqual(n.display("smb://nas.local/backup/Ivar%20sin/"), "nas.local / backup / Ivar sin")

    def test_usable_and_parent(self):
        from waypoint import network as n
        self.assertFalse(n.usable("smb://nas/", "smb"))      # selve serveren: velg en deling
        self.assertTrue(n.usable("smb://nas/backup/", "smb"))
        self.assertTrue(n.usable("sftp://u@nas/", "sftp"))
        self.assertIsNone(n.parent_uri("smb://nas/", "smb://nas/"))
        self.assertEqual(n.parent_uri("smb://nas/backup/ivar", "smb://nas/"), "smb://nas/backup/")

    def test_list_and_make_folder(self):
        from gi.repository import Gio
        from waypoint import network as n
        with tempfile.TemporaryDirectory() as d:
            Path(d, "b").mkdir()
            Path(d, "a [1]").mkdir()
            Path(d, ".skjult").mkdir()
            Path(d, "fil.txt").write_text("x")
            root = Gio.File.new_for_path(d).get_uri()
            self.assertEqual([f["name"] for f in n.list_folders(root)], ["a [1]", "b"])
            made = n.make_folder(root, "Waypoint-kopier")
            self.assertTrue(Path(d, "Waypoint-kopier").is_dir())
            self.assertEqual(n.local_path(made), str(Path(d, "Waypoint-kopier")))

    def test_describe_storage(self):
        from waypoint import repo
        self.assertEqual(repo.describe({"kind": "local", "path": "/run/media/u/Disk", "uri": "file:///run/media/u/Disk"}),
                         "/run/media/u/Disk")
        self.assertEqual(repo.describe({"kind": "local", "path": "/run/user/1000/gvfs/x",
                                        "uri": "smb://nas/backup/"}), "nas / backup")
        self.assertTrue(repo.Repo({"uri": "sftp://u@nas/x/"}, None).remote)
        self.assertFalse(repo.Repo({"uri": "file:///x"}, None).remote)


@unittest.skipUnless(RESTIC, "restic er ikke installert")
class ResticTwoJobsTests(unittest.TestCase):
    """To oppgaver mot samme lager: hver får sine egne tidspunkter (merkelappen job=…), og «Bare personlige
    filer» tar ikke med innstillingsfilene."""

    def test_two_jobs_same_storage(self):
        code = f"""
import sys, json; sys.path.insert(0, {str(ROOT)!r})
from waypoint import auto, settings, repo
r = repo.Repo.for_job(settings.job("files")); r.init()
print(auto.run(force=True, log=lambda t: None))
print(json.dumps({{j: [settings.job_status(j).get("state"), len(r.snapshots(job_id=j))] for j in ("files", "setup")}}))
snap = r.snapshots(job_id="files")[0]
print(json.dumps([p.rsplit("/", 1)[-1] for p in snap["paths"]]))
"""
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as store:
            storage = {"kind": "local", "path": store}
            Path(home, ".config/waypoint").mkdir(parents=True)
            Path(home, ".config/waypoint/.migrated-from-flytt").touch()
            Path(home, ".config/waypoint/settings.json").write_text(__import__("json").dumps({
                "tray": False, "skip_on_battery": False, "check_days": 0, "jobs": [
                    {"id": "files", "name": "Filer", "enabled": True, "preset": "files", "storage": storage},
                    {"id": "setup", "name": "Oppsett", "enabled": True, "preset": "setup", "storage": storage}]}))
            Path(home, "Dokumenter").mkdir()
            Path(home, "Dokumenter/a.txt").write_text("a")
            Path(home, ".config/app").mkdir(parents=True)
            Path(home, ".config/app/x.ini").write_text("x")
            env = {**os.environ, "HOME": home, "XDG_CONFIG_HOME": home + "/.config", "XDG_STATE_HOME": home + "/.st",
                   "XDG_CACHE_HOME": home + "/.cache", "WAYPOINT_PASSWORD": "passord123"}
            r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=600)
            self.assertEqual(r.returncode, 0, r.stderr)
            lines = r.stdout.strip().splitlines()
            self.assertEqual(lines[0], "0")
            self.assertEqual(__import__("json").loads(lines[1]), {"files": ["ok", 1], "setup": ["ok", 1]})
            paths = __import__("json").loads(lines[2])
            self.assertIn("Dokumenter", paths)
            self.assertNotIn("app", paths)  # innstillingsfilene hører til oppgaven «Oppsett»


class JobTests(unittest.TestCase):
    """Sikkerhetskopioppgaver: forhåndsvalg, overgang fra det gamle oppsettet og status per oppgave."""

    def _sections(self):
        from waypoint.model import Group, Item, Section
        personal = Group("files-personal", "p", items=[Item(id="Dokumenter", label="d", selected=True)])
        apps = Group("files-apps", "a", items=[Item(id=".config/x", label="x", selected=True),
                                               Item(id=".config/stor", label="s", selected=False)])
        settings_g = Group("settings", "s", flat=True, items=[Item(id="desktop", label="d", selected=True)])
        return [Section("A", "", [settings_g]), Section("B", "", [personal, apps])]

    def _selected(self, sections):
        return sorted(i.id for s in sections for g in s.groups for i in g.selected())

    def test_presets(self):
        from waypoint.model import apply_job, collect_choices
        s = self._sections()
        apply_job(s, {"preset": "files"})
        self.assertEqual(self._selected(s), ["Dokumenter"])
        s = self._sections()
        apply_job(s, {"preset": "setup"})
        self.assertEqual(self._selected(s), [".config/x", "desktop"])
        s = self._sections()
        apply_job(s, {"preset": "all"})
        self.assertEqual(self._selected(s), [".config/x", "Dokumenter", "desktop"])
        # Egne valg lagres som avvik fra grunnvalget, og legges på igjen
        s = self._sections()
        apply_job(s, {"preset": "setup"})
        s[1].groups[1].items[1].selected = True
        choices = collect_choices(s)
        self.assertEqual(choices, {"files-apps": {".config/stor": True}})
        s = self._sections()
        apply_job(s, {"preset": "setup", "choices": choices})
        self.assertEqual(self._selected(s), [".config/stor", ".config/x", "desktop"])

    def test_migrate_old_storage_to_job(self):
        out = ReviewFixTests._isolated(self, """
import json
from waypoint import settings
settings.write_json(settings.config_dir() / "settings.json",
                    {"storage": {"kind": "local", "path": "/mnt/x"}, "auto_enabled": True, "auto_interval": "hourly",
                     "choices": {"files-apps": {"a": False}}})
settings.update_status(state="ok", last_success="2026-11-15T12:00:00+01:00", update_checked="x")
settings.migrate_jobs()
settings.migrate_jobs()  # andre gang: ingenting skjer
jobs = settings.jobs()
st = settings.read_status()
print(json.dumps([len(jobs), jobs[0]["storage"]["path"], jobs[0]["enabled"], jobs[0]["interval"],
                  jobs[0]["choices"], settings.get("storage"), st["update_checked"],
                  settings.job_status(jobs[0]["id"])["last_success"]]))
""")
        import json
        self.assertEqual(json.loads(out), [1, "/mnt/x", True, "hourly", {"files-apps": {"a": False}}, None, "x",
                                           "2026-11-15T12:00:00+01:00"])

    def test_save_and_delete_job(self):
        out = ReviewFixTests._isolated(self, """
from waypoint import settings
a = {"id": "a", "name": "Filer"}
b = {"id": "b", "name": "Oppsett"}
settings.save_job(a); settings.save_job(b)
settings.save_job({"id": "a", "name": "Filer 2"})   # redigering beholder plassen i listen
settings.update_job_status("a", state="ok"); settings.update_job_status("b", state="ok")
settings.delete_job("b")
print([j["name"] for j in settings.jobs()], list(settings.read_status()["jobs"]))
""")
        self.assertIn("['Filer 2'] ['a']", out)


class Review2Tests(unittest.TestCase):
    """Tester for funnene i kodesjekk 2 (REVIEW.md)."""

    def _isolated(self, code: str, extra_env: dict | None = None) -> str:
        return ReviewFixTests._isolated(self, code) if not extra_env else self._run_env(code, extra_env)

    def _run_env(self, code, extra_env):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home, "XDG_CONFIG_HOME": home + "/.config", "XDG_STATE_HOME": home + "/.st",
                   **extra_env}
            r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r})\n" + code],
                               env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout

    def test_password_locked_when_jobs_use_it(self):  # funn 1
        out = self._run_env("""
from waypoint import settings
print(settings.password_in_use())
settings.save_job({"id": "a", "name": "x"})
print(settings.password_in_use())
settings.save_job({"id": "b", "name": "y", "storage": {"kind": "local", "path": "/x"}})
print(settings.password_in_use())
""", {"WAYPOINT_PASSWORD": "passord123"})
        self.assertEqual(out.split(), ["False", "False", "True"])

    def test_storage_root_skips_repo_folder(self):  # funn 3 (valgte selve «Waypoint»-mappen)
        from waypoint import repo
        with tempfile.TemporaryDirectory() as d:
            Path(d, "Waypoint").mkdir()
            self.assertEqual(repo.storage_root(d + "/Waypoint")[0], d + "/Waypoint")  # ikke et lager
            Path(d, "Waypoint/config").write_text("x")
            path, uri = repo.storage_root(d + "/Waypoint", "file://" + d + "/Waypoint")
            self.assertEqual((path, uri), (d, "file://" + d))
            self.assertEqual(repo.storage_root(d)[0], d)

    def test_space_candidates_include_orphans(self):  # funn 4
        from waypoint import auto
        snaps = [{"id": "old", "time": "2026-01-01", "tags": ["waypoint"]},
                 {"id": "a1", "time": "2026-02-01", "tags": ["waypoint", "job=a"]},
                 {"id": "gone", "time": "2026-03-01", "tags": ["waypoint", "job=deleted"]},
                 {"id": "b1", "time": "2026-04-01", "tags": ["waypoint", "job=b"]},
                 {"id": "a2", "time": "2026-05-01", "tags": ["waypoint", "job=a"]}]
        ids = [s["id"] for s in auto.space_candidates(snaps, "a", {"deleted"})]
        self.assertEqual(ids, ["old", "a1", "gone"])  # aldri a2 (nyeste) eller b1 (en annen oppgave)

    def test_space_candidates_never_unknown_jobs(self):  # M2: ukjente oppgaver slettes aldri automatisk
        from waypoint import auto
        snaps = [{"id": "foreign", "time": "2026-01-01", "tags": ["waypoint", "job=fra-gammel-installasjon"]},
                 {"id": "gone", "time": "2026-02-01", "tags": ["waypoint", "job=deleted"]},
                 {"id": "a1", "time": "2026-03-01", "tags": ["waypoint", "job=a"]},
                 {"id": "a2", "time": "2026-04-01", "tags": ["waypoint", "job=a"]}]
        ids = [s["id"] for s in auto.space_candidates(snaps, "a", {"deleted"})]
        self.assertEqual(ids, ["gone", "a1"])
        self.assertEqual([s["id"] for s in auto.space_candidates(snaps, "a", set())], ["a1"])

    def test_deleted_jobs_remembered(self):  # M2
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": home + "/c", "XDG_STATE_HOME": home + "/s"}):
            from waypoint import settings
            settings.save_job({"id": "x1", "name": "x"})
            settings.save_job({"id": "x2", "name": "y"})
            self.assertEqual(settings.deleted_job_ids(), set())
            settings.delete_job("x1")
            settings.delete_job("x1")
            self.assertEqual(settings.deleted_job_ids(), {"x1"})
            self.assertEqual([j["id"] for j in settings.jobs()], ["x2"])

    def test_queue_cleared_only_when_nobody_runs(self):  # funn 5
        out = self._isolated("""
import fcntl, subprocess, sys, time
from waypoint import settings
settings.save_job({"id": "q", "name": "x"})
settings.update_job_status("q", queued=True)
lock = settings.state_dir() / "auto.lock"
holder = subprocess.Popen([sys.executable, "-c",
    "import fcntl,sys,time; f=open(sys.argv[1],'w'); fcntl.flock(f, fcntl.LOCK_EX); print('ok', flush=True); "
    "time.sleep(30)", str(lock)], stdout=subprocess.PIPE, text=True)
holder.stdout.readline()
print(settings.job_status("q")["queued"])   # noen kjører: fortsatt i kø
holder.kill(); holder.wait()
print(settings.job_status("q")["queued"])   # ingen kjører: ut av køen
""")
        self.assertEqual(out.split(), ["True", "False"])

    def test_stale_after_reboot(self):  # mindre funn: gjenbrukt prosess-id
        out = self._isolated("""
import os
from waypoint import settings
settings.save_job({"id": "r", "name": "x"})
settings.update_job_status("r", state="running", pid=os.getpid(), boot="en-annen-oppstart")
print(settings.job_status("r")["state"])
settings.update_job_status("r", state="running", pid=os.getpid(), boot=settings.boot_id())
print(settings.job_status("r")["state"])
""")
        self.assertEqual(out.split(), ["error", "running"])

    def test_deleted_job_gets_no_status_and_files_removed(self):  # funn 7
        out = self._isolated("""
from waypoint import settings
settings.save_job({"id": "d", "name": "x"})
(settings.state_dir() / "setup-d").mkdir(parents=True)
(settings.state_dir() / "excludes-d.txt").write_text("x")
settings.delete_job("d")
settings.update_job_status("d", state="running", progress=0.5)   # kopien som fortsatt kjører
print("d" in settings.read_status()["jobs"], (settings.state_dir() / "setup-d").exists(),
      (settings.state_dir() / "excludes-d.txt").exists())
""")
        self.assertEqual(out.split(), ["False", "False", "False"])

    def test_overdue_warning_while_waiting_for_power(self):  # funn 8
        out = self._isolated("""
from waypoint import auto, settings
settings.save_job({"id": "w", "name": "Filer", "enabled": True, "interval": "daily",
                   "storage": {"kind": "local", "path": "/finnes/ikke"}})
settings.update_job_status("w", last_success="2026-01-01T12:00:00+01:00")
settings.set("skip_on_battery", True)
auto.on_battery = lambda: True
sent = []
auto.notify = lambda title, body: sent.append(title)
auto.run(log=lambda t: None)
auto.run(log=lambda t: None)   # samme døgn: ikke nytt varsel
print(len(sent), settings.job_status("w")["state"])
""")
        self.assertEqual(out.split(), ["1", "waiting_power"])

    def test_only_queued_jobs_run_after_lock_release(self):  # funn 5 (kappløpet ved slipp av låsen)
        out = self._isolated("""
from waypoint import auto, settings
for jid in ("a", "b"):
    settings.save_job({"id": jid, "name": jid, "enabled": False})
settings.update_job_status("b", queued=True)
auto._run(False, lambda t: None, None, only_queued=True)
print(bool(settings.job_status("a").get("last_attempt")), bool(settings.job_status("b").get("last_attempt")),
      settings.job_status("b").get("queued"))
""")
        self.assertEqual(out.split(), ["False", "True", "False"])


@unittest.skipUnless(RESTIC, "restic er ikke installert")
class ResticAdoptTests(unittest.TestCase):
    """Funn 4: tidspunkter uten job=-merkelapp (fra før oppgavene) knyttes til oppgaven som er alene om stedet."""

    def test_untagged_snapshots_are_adopted(self):
        code = f"""
import sys, json; sys.path.insert(0, {str(ROOT)!r})
from pathlib import Path
from waypoint import auto, settings, repo
job = settings.job("solo")
r = repo.Repo.for_job(job); r.init()
r.backup([str(Path.home() / "Dokumenter")], None, [], lambda f, c: None, lambda: False)   # gammel kopi uten job=
print(len(r.snapshots(job_id="solo")))
print(auto.run(force=True, log=lambda t: None))
print(len(r.snapshots(job_id="solo")), len([s for s in r.snapshots() if repo.snapshot_job(s) is None]))
"""
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as store:
            Path(home, ".config/waypoint").mkdir(parents=True)
            Path(home, ".config/waypoint/.migrated-from-flytt").touch()
            Path(home, ".config/waypoint/settings.json").write_text(__import__("json").dumps({
                "tray": False, "skip_on_battery": False, "check_days": 0, "jobs": [
                    {"id": "solo", "name": "Filer", "enabled": True, "preset": "files",
                     "storage": {"kind": "local", "path": store}}]}))
            Path(home, "Dokumenter").mkdir()
            Path(home, "Dokumenter/a.txt").write_text("a")
            env = {**os.environ, "HOME": home, "XDG_CONFIG_HOME": home + "/.config", "XDG_STATE_HOME": home + "/.st",
                   "XDG_CACHE_HOME": home + "/.cache", "WAYPOINT_PASSWORD": "passord123"}
            r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=600)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.split(), ["0", "0", "2", "0"])


class ScheduleTests(unittest.TestCase):
    """Fast klokkeslett for automatiske kopier."""

    def t(self, iso):
        from datetime import datetime
        return datetime.fromisoformat(iso).astimezone()

    def test_daily_at_fixed_time(self):
        from waypoint import auto
        job = {"enabled": True, "interval": "daily", "at": "02:00"}
        st = {"last_success": "2026-11-15T02:05:00+01:00"}
        self.assertFalse(auto.due(job, st, self.t("2026-11-16T01:30:00+01:00")))   # før 02:00
        self.assertTrue(auto.due(job, st, self.t("2026-11-16T02:10:00+01:00")))    # etter 02:00
        self.assertTrue(auto.due(job, st, self.t("2026-11-16T09:00:00+01:00")))    # maskinen var av kl. 02
        done = {"last_success": "2026-11-16T09:01:00+01:00"}
        self.assertFalse(auto.due(job, done, self.t("2026-11-16T23:00:00+01:00")))  # tatt i dag, vent til i morgen
        self.assertEqual(str(auto.next_time(job, done, self.t("2026-11-16T23:00:00+01:00"))), "2026-11-17 02:00:00")

    def test_weekly_on_weekday(self):
        from waypoint import auto
        job = {"enabled": True, "interval": "weekly", "at": "21:30", "weekday": 6}   # søndag
        st = {"last_success": "2026-11-15T21:40:00+01:00"}                          # søndag 15.11.
        self.assertFalse(auto.due(job, st, self.t("2026-11-21T22:00:00+01:00")))    # lørdag
        self.assertTrue(auto.due(job, st, self.t("2026-11-22T21:31:00+01:00")))     # søndag etter 21:30
        self.assertEqual(str(auto.next_time(job, st, self.t("2026-11-16T09:00:00+01:00"))), "2026-11-22 21:30:00")

    def test_first_backup_waits_for_the_time(self):
        from waypoint import auto
        job = {"enabled": True, "interval": "daily", "at": "02:00"}
        st = {"enabled_since": "2026-11-16T10:00:00+01:00"}                          # slått på kl. 10
        self.assertFalse(auto.due(job, st, self.t("2026-11-16T15:00:00+01:00")))
        self.assertTrue(auto.due(job, st, self.t("2026-11-17T02:05:00+01:00")))

    def test_timer_wakes_at_fixed_times(self):
        from waypoint import auto
        jobs = [{"enabled": True, "interval": "daily", "at": "11:45"},
                {"enabled": True, "interval": "weekly", "at": "21:30", "weekday": 6},
                {"enabled": False, "interval": "daily", "at": "03:00"},     # ikke automatisk
                {"enabled": True, "interval": "hourly", "at": "04:00"},     # klokkeslett gjelder ikke
                {"enabled": True, "interval": "daily"}]
        cal = auto.calendars(jobs)
        self.assertEqual(cal, ["*-*-* 11:45:00", "Sun *-*-* 21:30:00"])
        unit = auto.timer_unit(cal)
        self.assertIn("OnUnitActiveSec=15min", unit)
        self.assertIn("OnCalendar=Sun *-*-* 21:30:00\n", unit)
        for c in cal:
            self.assertEqual(subprocess.run(["systemd-analyze", "calendar", c], capture_output=True).returncode, 0)

    def test_without_fixed_time_and_hourly_unchanged(self):
        from waypoint import auto
        st = {"last_success": "2026-11-16T08:00:00+01:00"}
        daily = {"enabled": True, "interval": "daily"}
        self.assertFalse(auto.due(daily, st, self.t("2026-11-16T20:00:00+01:00")))
        self.assertTrue(auto.due(daily, st, self.t("2026-11-17T08:00:00+01:00")))
        hourly = {"enabled": True, "interval": "hourly", "at": "02:00"}             # klokkeslett gjelder ikke
        self.assertTrue(auto.due(hourly, st, self.t("2026-11-16T09:00:00+01:00")))
        self.assertIsNone(auto.fixed_time(hourly))


class UpdaterTests(unittest.TestCase):
    def _tar(self, d: Path, version: str, extra=None) -> Path:
        import tarfile
        src = d / "waypoint-x"
        (src / "waypoint").mkdir(parents=True)
        (src / "install.sh").write_text("#!/bin/sh\n")
        (src / "waypoint/__init__.py").write_text(f'__version__ = "{version}"\n')
        archive = d / "a.tar.gz"
        with tarfile.open(archive, "w:gz") as t:
            t.add(src, arcname="waypoint-x")
            if extra:
                extra(t)
        return archive

    def test_versions(self):
        from waypoint import updater
        self.assertTrue(updater.is_newer("v9.0.0"))
        self.assertFalse(updater.is_newer("v0.0.1"))
        self.assertFalse(updater.is_newer("ugyldig"))

    def test_extract_checks_version_and_content(self):
        from waypoint import updater
        with tempfile.TemporaryDirectory() as d:
            src = updater.extract(self._tar(Path(d), "1.2.3"), Path(d) / "ok", "v1.2.3")
            self.assertTrue((src / "install.sh").exists())
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(updater.UpdateError):
                updater.extract(self._tar(Path(d), "1.2.2"), Path(d) / "x", "v1.2.3")

        def symlink(t):
            import tarfile
            info = tarfile.TarInfo("waypoint-x/lenke")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            t.addfile(info)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(updater.UpdateError):
                updater.extract(self._tar(Path(d), "1.2.3", symlink), Path(d) / "x", "v1.2.3")


class UpdateWhileBusyTests(unittest.TestCase):
    """H1: oppdateringen skal ikke bytte ut koden mens en sikkerhetskopi kjører."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        home = self.tmp.name
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": home + "/.config",
                                                "XDG_STATE_HOME": home + "/.st"})
        self.env.start()
        from waypoint import settings
        settings.save_job({"id": "j1", "name": "Test", "enabled": True, "interval": "daily", "preset": "files"})
        settings.set("update_auto", True)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _maybe_update(self):
        from waypoint import auto, updater
        newer = {"tag": "v99.0.0", "version": "99.0.0", "name": "", "notes": "", "url": ""}
        with mock.patch.object(updater, "fetch_latest", return_value=newer), \
                mock.patch.object(updater, "is_installed", return_value=True), \
                mock.patch.object(updater, "_install") as inst, \
                mock.patch.object(updater, "restart_tray"), \
                mock.patch.object(auto, "notify"), mock.patch.object(auto, "notify_once"):
            result = updater.maybe_update(log=lambda t: None, force=True)
        return result, inst

    def test_not_while_job_running(self):
        from waypoint import settings
        settings.update_job_status("j1", state="running", pid=os.getpid(), boot=settings.boot_id())
        result, inst = self._maybe_update()
        inst.assert_not_called()
        self.assertIsNone(result)

    def test_not_while_lock_held_by_other_process(self):
        import subprocess
        from waypoint import settings, updater
        settings.state_dir().mkdir(parents=True, exist_ok=True)
        lock = settings.state_dir() / "auto.lock"
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import fcntl, sys, time; f = open(sys.argv[1], 'w'); "
                                   "fcntl.flock(f, fcntl.LOCK_EX); print('ok', flush=True); time.sleep(30)",
                                   str(lock)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "ok")
            self.assertTrue(updater.busy())
            result, inst = self._maybe_update()
            inst.assert_not_called()
            self.assertIsNone(result)
            with mock.patch.object(updater, "is_installed", return_value=True), \
                    mock.patch.object(updater, "_install") as inst2:
                with self.assertRaises(updater.Busy):
                    updater.install("v99.0.0")
                inst2.assert_not_called()
        finally:
            holder.kill()
            holder.wait()

    def test_installs_when_idle_and_holds_lock(self):
        from waypoint import updater
        self.assertFalse(updater.busy())
        seen = []
        with mock.patch.object(updater, "is_installed", return_value=True), \
                mock.patch.object(updater, "_install", side_effect=lambda *a: seen.append(updater.busy())):
            updater.install("v99.0.0")
        self.assertEqual(seen, [True])       # låsen holdes mens install.sh kjører
        self.assertFalse(updater.busy())     # og slippes etterpå
        result, inst = self._maybe_update()
        inst.assert_called_once()
        self.assertEqual(result, "v99.0.0")


class UndefinedNameTests(unittest.TestCase):
    """Enkel erstatning for pyflakes: et navn som leses et sted i en modul, må være definert et sted i den
    (import, def, class, tilordning, parameter) eller være innebygd. Fanger f.eks. en konstant som er slettet."""

    def test_no_undefined_names(self):
        import ast
        import builtins
        known_extra = {"__file__", "__name__", "__doc__", "__spec__", "__loader__", "__builtins__"}
        problems = []
        for path in sorted((ROOT / "waypoint").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            defined = set(dir(builtins)) | known_extra
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    defined |= {(a.asname or a.name).split(".")[0] for a in node.names}
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    defined.add(node.name)
                elif isinstance(node, ast.arg):
                    defined.add(node.arg)
                elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    defined.add(node.id)
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    defined.add(node.name)
                elif isinstance(node, (ast.Global, ast.Nonlocal)):
                    defined |= set(node.names)
                elif isinstance(node, ast.MatchAs) and node.name:
                    defined.add(node.name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in defined:
                    problems.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.id}")
        self.assertEqual(problems, [])


class TranslationTests(unittest.TestCase):
    def test_every_string_translated(self):
        sys.path.insert(0, str(ROOT / "tools"))
        from extract_strings import strings
        cwd = os.getcwd()
        os.chdir(ROOT)
        try:
            found = strings()
        finally:
            os.chdir(cwd)
        from waypoint.locales import de, en, es, fr
        for s in found:
            self.assertIn(s, en.T, s)
            for table in (de.T, es.T, fr.T):
                self.assertIn(en.T[s], table, s)
                self.assertEqual(en.T[s].count("{}"), table[en.T[s]].count("{}"), s)


if __name__ == "__main__":
    unittest.main()
