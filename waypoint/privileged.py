#!/usr/bin/env python3
"""Kjøres som root via pkexec: legger til pakkekilder og installerer systempakker.

Leser en plan (JSON) fra filen i argv[1]. Skriver fremdrift til stdout som vanlige linjer, og
statuslinjer som begynner med «@@» som Waypoint tolker:
  @@STEP <tekst>          en ny del begynner
  @@OK <pakke>            pakken er installert
  @@FAIL <pakke>          pakken kunne ikke installeres
Denne filen er bevisst uavhengig av resten av Waypoint (pkexec fjerner PYTHONPATH).
"""

import json
import os
import re
import subprocess
import sys

# Planen kommer fra en sikkerhetskopi, som kan være laget et annet sted. Alt som sendes til pakkesystemet som root,
# må derfor se ut som et pakkenavn, en adresse eller et filnavn – aldri som en opsjon.
PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*(:[A-Za-z0-9_-]+)?")
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]*")
URL = re.compile(r"(https?|ftp)://[^\s'\"]+")
REPO_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.repo")


def valid_name(text):
    return isinstance(text, str) and "\n" not in text and not text.startswith("-") and 0 < len(text) < 200


def say(text):
    print(text, flush=True)


def run(cmd):
    say("$ " + " ".join(cmd))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         env={**os.environ, "LC_ALL": "C.UTF-8"})
    for line in p.stdout:
        say(line.rstrip())
    return p.wait()


def install(mgr, pkgs):
    """Prøver alle på én gang; hvis det feiler, én og én så én dårlig pakke ikke stopper resten."""
    base = {
        "zypper": ["zypper", "--non-interactive", "--gpg-auto-import-keys", "install", "--auto-agree-with-licenses"],
        "dnf": ["dnf", "install", "-y"],
        "apt": ["apt-get", "install", "-y"],
        "pacman": ["pacman", "-S", "--needed", "--noconfirm"],
    }[mgr]
    if not pkgs:
        return
    if mgr != "apt":  # apt-get kjenner ikke «--»; navnene er uansett kontrollert (kan ikke starte med «-»)
        base = base + ["--"]  # alt etter dette er pakkenavn, aldri opsjoner
    if run(base + pkgs) == 0:
        for p in pkgs:
            say("@@OK " + p)
        return
    say("Some packages could not be installed together – trying one at a time.")
    for p in pkgs:
        say(("@@OK " if run(base + [p]) == 0 else "@@FAIL ") + p)


def main():
    with open(sys.argv[1]) as f:
        plan = json.load(f)
    mgr = plan["manager"]
    if mgr not in ("zypper", "dnf", "apt", "pacman"):
        say("Unknown package manager: " + mgr)
        return 2

    repos = plan.get("repos", [])
    if repos:
        say("@@STEP Legger til pakkekilder")
        for r in repos:
            if mgr == "zypper":
                url, alias, name = r.get("url", ""), r.get("alias", ""), r.get("name") or r.get("alias", "")
                if not (URL.fullmatch(url) and ALIAS.fullmatch(alias) and valid_name(name)):
                    say("Skipping repository with an unexpected name or address: " + repr(alias))
                    continue
                try:
                    priority = str(int(r.get("priority", 99)))
                except (TypeError, ValueError):
                    priority = "99"
                if r.get("gpgkey") and URL.fullmatch(r["gpgkey"]):
                    run(["rpm", "--import", "--", r["gpgkey"]])
                run(["zypper", "--non-interactive", "addrepo", "--refresh", "--priority", priority,
                     "--name", name, "--", url, alias])
            elif mgr == "dnf" and r.get("content") and r.get("file"):
                name = os.path.basename(r["file"])
                if not REPO_FILE.fullmatch(name):
                    say("Skipping repository file with an unexpected name: " + repr(name))
                    continue
                path = os.path.join("/etc/yum.repos.d", name)
                if not os.path.exists(path):
                    say("Writing " + path)
                    with open(path, "w") as f:
                        f.write(r["content"])
        if mgr == "zypper":
            run(["zypper", "--non-interactive", "--gpg-auto-import-keys", "refresh"])
    if mgr == "apt":
        run(["apt-get", "update"])
    elif mgr == "pacman":
        run(["pacman", "-Sy", "--noconfirm"])

    pkgs = plan.get("packages", [])
    bad = [p for p in pkgs if not (isinstance(p, str) and PACKAGE.fullmatch(p))]
    for p in bad:
        say("Skipping unexpected package name: " + repr(p))
        say("@@FAIL " + str(p))
    pkgs = [p for p in pkgs if p not in bad]
    if pkgs:
        say("@@STEP Installerer pakker")
        install(mgr, pkgs)
    return 0


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("Must run as root (via pkexec).", file=sys.stderr)
        sys.exit(1)
    sys.exit(main())
