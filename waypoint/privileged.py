#!/usr/bin/python3 -I
"""Kjøres som root via pkexec: legger til pakkekilder og installerer systempakker.

install.sh legger denne filen inn som root-eid /usr/local/libexec/waypoint/waypoint-privileged, med en egen
polkit-regel (io.github.waypoint.Waypoint.policy), så ingen program som kjører som brukeren kan bytte den ut og
vente på administratorpassordet. Oppdatering fra appen (uten root) kan ikke oppdatere den: øk HELPER_VERSION når
filen endres, så ber Waypoint brukeren kjøre install.sh på nytt.

  waypoint-privileged <plan.json>   kjør planen
  waypoint-privileged --version     skriv HELPER_VERSION
  waypoint-privileged --uninstall   fjern hjelperen og polkit-regelen (brukes av uninstall.sh)

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

HELPER_VERSION = 1
HELPER_PATH = "/usr/local/libexec/waypoint/waypoint-privileged"
POLICY_PATH = "/usr/share/polkit-1/actions/io.github.waypoint.Waypoint.policy"

# Planen kommer fra en sikkerhetskopi, som kan være laget et annet sted. Alt som sendes til pakkesystemet som root,
# må derfor se ut som et pakkenavn, en adresse eller et filnavn – aldri som en opsjon.
PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*(:[A-Za-z0-9_-]+)?")
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]*")
URL = re.compile(r"(https?|ftp)://[^\s'\"]+")
REPO_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.repo")


# Nøklene som skrives tilbake i en .repo-fil for dnf; alt annet i filen fra kopien droppes.
DNF_KEYS = ("name", "baseurl", "metalink", "gpgkey", "enabled", "gpgcheck", "repo_gpgcheck", "type",
            "skip_if_unavailable", "priority", "cost", "metadata_expire", "enabled_metadata")
SIMPLE_VALUE = re.compile(r"[A-Za-z0-9._-]+")
LOCAL_KEY = re.compile(r"file:///etc/pki/rpm-gpg/[A-Za-z0-9._-]+")
FALSE = ("0", "false", "no", "off")


def valid_dnf_repo(text):
    """Kontrollerer en .repo-fil fra kopien og returnerer en ren versjon med bare kjente nøkler, eller None.
    Nektes: mer enn én seksjon, gpgcheck=0 eller repo_gpgcheck=0, ingen baseurl/metalink, eller adresser som
    ikke ser ut som http(s)/ftp. Ren funksjon, så den kan testes uten root."""
    import configparser
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read_string(text if isinstance(text, str) else "")
    except configparser.Error:
        return None
    if len(cp.sections()) != 1:
        return None
    section = cp.sections()[0]
    if not ALIAS.fullmatch(section):
        return None
    sec = cp[section]
    for key in ("gpgcheck", "repo_gpgcheck"):
        if key in sec and sec[key].strip().lower() in FALSE:
            return None
    lines = ["[" + section + "]"]
    has_url = False
    for key in DNF_KEYS:
        if key not in sec:
            continue
        value = sec[key].strip()
        if key in ("baseurl", "gpgkey"):
            parts = value.split()
            ok = URL.fullmatch if key == "baseurl" else (lambda u: URL.fullmatch(u) or LOCAL_KEY.fullmatch(u))
            if not parts or not all(ok(u) for u in parts):
                return None
            value = " ".join(parts)
            has_url = has_url or key == "baseurl"
        elif key == "metalink":
            if not URL.fullmatch(value):
                return None
            has_url = True
        elif key == "name":
            if not valid_name(value):
                return None
        elif not SIMPLE_VALUE.fullmatch(value):
            return None
        lines.append(key + "=" + value)
    if not has_url:
        return None
    return "\n".join(lines) + "\n"


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
        # Ingen --gpg-auto-import-keys: nøkler godtas bare for kilder brukeren har valgt (se main).
        "zypper": ["zypper", "--non-interactive", "install", "--auto-agree-with-licenses"],
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


def uninstall():
    for path in (HELPER_PATH, POLICY_PATH):
        try:
            os.unlink(path)
            say("Removed " + path)
        except FileNotFoundError:
            pass
    try:
        os.rmdir(os.path.dirname(HELPER_PATH))
    except OSError:
        pass
    return 0


def main():
    if len(sys.argv) != 2:
        say("Usage: waypoint-privileged <plan.json> | --version | --uninstall")
        return 2
    if sys.argv[1] == "--version":
        say(str(HELPER_VERSION))
        return 0
    if sys.argv[1] == "--uninstall":
        return uninstall()
    with open(sys.argv[1]) as f:
        plan = json.load(f)
    mgr = plan["manager"]
    if mgr not in ("zypper", "dnf", "apt", "pacman"):
        say("Unknown package manager: " + mgr)
        return 2

    repos = plan.get("repos", [])
    if repos:
        say("@@STEP Legger til pakkekilder")
        added = []
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
                if run(["zypper", "--non-interactive", "addrepo", "--refresh", "--priority", priority,
                        "--name", name, "--", url, alias]) == 0:
                    added.append(alias)
            elif mgr == "dnf" and r.get("content") and r.get("file"):
                name = os.path.basename(r["file"])
                if not REPO_FILE.fullmatch(name):
                    say("Skipping repository file with an unexpected name: " + repr(name))
                    continue
                content = valid_dnf_repo(r["content"])
                if content is None:
                    say("Skipping repository file that is not a single, signed repository: " + repr(name))
                    continue
                path = os.path.join("/etc/yum.repos.d", name)
                if not os.path.exists(path):
                    say("Writing " + path)
                    with open(path, "w") as f:
                        f.write(content)
        # Bare kildene brukeren har valgt og som ble lagt til nå, får nøkkelen sin godtatt uten spørsmål (zypper
        # spør ellers, og --non-interactive svarer nei). Nøkler fra rpm --import over er allerede godtatt.
        for alias in added:
            run(["zypper", "--non-interactive", "--gpg-auto-import-keys", "refresh", "--", alias])
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
