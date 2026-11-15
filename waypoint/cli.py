"""Command line. "waypoint" without arguments opens the window.

  waypoint                    open the window
  waypoint <file>             open the window with an exported file ready to restore
  waypoint preferences        open the settings
  waypoint browse             recover single files from an earlier point in time
  waypoint check              verify the backups on every storage location
  waypoint snapshots          list the points in time on each storage location
  waypoint list [--job ID]    show what would be backed up (for a task)
  waypoint jobs               list the backup tasks
  waypoint backup [--job ID]  back up now (one task, or all)
  waypoint export [file]      export your setup to a single file (encrypted with the password in the keyring)
  waypoint inspect <file>     show what an exported file contains and what is missing on this computer
  waypoint auto [--now] [--job ID]   automatic backups (run by the systemd timer)
  waypoint tray               the system tray icon
"""

import sys
from pathlib import Path


def _size(n: float) -> str:
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return ""


def _print(sections):
    for s in sections:
        print(f"\n== {s.title}")
        for g in s.groups:
            sel = g.selected()
            print(f"  {g.title}: {len(sel)}/{len(g.items)}" + (f"  ({g.note})" if g.note else ""))
            for i in g.items:
                mark = "x" if i.selected and i.available else ("-" if not i.available else " ")
                size = f"  {_size(i.size)}" if i.size else ""
                lock = " [enc]" if i.sensitive else ""
                print(f"    [{mark}] {i.label}{size}{lock}  {i.detail[:70]}")


def _opt(argv: list[str], name: str) -> str | None:
    """Verdien etter «--navn», f.eks. «--job 1a2b3c4d»."""
    if name in argv and argv.index(name) + 1 < len(argv):
        return argv[argv.index(name) + 1]
    return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    from . import migrate, settings as _settings
    migrate.run()
    _settings.migrate_jobs()
    if not argv or argv[0].endswith((".tar.gz", ".gpg")):
        from .app import main as app_main
        return app_main(argv)
    cmd = argv[0]
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if cmd in ("preferences", "browse", "progress"):
        from .app import main as app_main
        return app_main([], action=cmd)
    if cmd == "check":
        from . import auto
        return 0 if auto.check_all() else 1
    if cmd == "jobs":
        from . import repo, settings
        for j in settings.jobs():
            st = settings.job_status(j["id"])
            print(j["id"], j.get("name"), j.get("preset", "all"), j.get("interval"),
                  "auto" if j.get("enabled") else "manual", repo.describe(j.get("storage")),
                  st.get("state", "-"), st.get("last_success", "-"))
        return 0
    if cmd == "snapshots":
        from . import repo
        storages = repo.Repo.all_storages()
        if not storages:
            print("No backup jobs", file=sys.stderr)
            return 1
        for r in storages:
            print("==", repo.describe(r.cfg))
            for s in r.snapshots():
                summ = s.get("summary") or {}
                job = next((t[4:] for t in s.get("tags") or [] if t.startswith("job=")), "")
                print(s["short_id"], s["time"][:19], s.get("hostname"), job, summ.get("total_files_processed", ""))
        return 0
    if cmd == "tray":
        from .tray import main as tray_main
        return tray_main()
    if cmd == "auto":
        from . import auto
        return auto.run(force="--now" in argv, job_id=_opt(argv, "--job"), interactive="--interactive" in argv)
    if cmd == "list":
        from . import backup, settings
        from .model import apply_job
        sections = backup.scan()
        job = settings.job(_opt(argv, "--job"))
        if job:
            apply_job(sections, job)
        _print(sections)
        return 0
    if cmd == "backup":
        from . import auto
        return auto.run(force=True, job_id=_opt(argv, "--job"))
    if cmd == "export":
        from . import backup, settings
        from .model import apply_choices
        sections = backup.scan(personal=False)
        password = settings.get_password()
        encrypt_all = bool(settings.get("encrypt_all")) and bool(password)
        if not password:
            for s in sections:
                for g in s.groups:
                    for i in g.items:
                        if i.sensitive:
                            i.selected = False
        dest = Path(argv[1]) if len(argv) > 1 else \
            backup.default_folder() / backup.default_filename(encrypted=encrypt_all)
        n, size = backup.estimate(sections)
        print(f"{n} → {dest} (~{_size(size)})" + (" [encrypted]" if encrypt_all else ""))
        last = [0]

        def progress(frac, text):
            pct = int(frac * 100)
            if pct >= last[0] + 10:
                last[0] = pct
                print(f"  {pct} %")
        backup.write_backup(sections, dest, password, encrypt_all, progress)
        print(f"OK: {dest} ({_size(dest.stat().st_size)})")
        return 0
    if cmd == "inspect" and len(argv) > 1:
        import getpass
        from . import crypto, settings
        from .restore import BackupFile, BadBackup, NeedPassword
        path = Path(argv[1])
        password = settings.get_password() if crypto.is_encrypted(path) else None
        try:
            try:
                b = BackupFile(path, password)
            except (NeedPassword, crypto.BadPassword):
                b = BackupFile(path, getpass.getpass("Password: "))
        except (BadBackup, crypto.BadPassword, crypto.CryptoError) as e:
            print(e or "Wrong password", file=sys.stderr)
            return 1
        try:
            print(b.summary())
            _print(b.sections())
        finally:
            b.cleanup()
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
