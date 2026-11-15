# Waypoint

Sikkerhetskopi av Linux-oppsettet og filene dine, som Time Machine: innstillinger, utvidelser, bakgrunnsbilde,
programmer, dokumenter og bilder. Gå tilbake til et hvilket som helst tidspunkt, hent en enkelt fil, eller legg alt
tilbake etter en ny installasjon.

## Hva blir tatt med

| Del | Hvordan den gjenopprettes |
|---|---|
| GNOME-innstillinger (dconf): snarveier, tema, dock, mus, tastatur, programmapper | `dconf load`, i tre grupper du kan velge hver for seg |
| Bakgrunnsbilde og låseskjerm | Settes på nytt |
| GNOME-utvidelser | Fra kopien hvis de passer GNOME-versjonen, ellers ny versjon fra extensions.gnome.org |
| Systempakker du selv har installert (zypper, dnf, apt, pacman) og pakkekilder | Installeres på nytt med administratorpassord (pkexec) |
| Flatpak-apper og Flatpak-kilder | `flatpak install` |
| Personlige mapper: Dokumenter, Bilder, Musikk, Videoer, Nedlastinger og andre mapper i hjemmemappen | Legges tilbake |
| Innstillingsfiler: `~/.config`, skrifttyper, temaer, `.bashrc`, `.gitconfig`, `~/.local/bin`, Flatpak-data | Legges tilbake |
| Nøkler og passord (SSH, GPG, nøkkelring), nettleserprofiler | Alltid kryptert |

Mellomlager, låsefiler og det du legger inn under Unntak tas ikke med. Det som erstattes ved gjenoppretting,
flyttes til `~/.local/share/waypoint/replaced/<tidspunkt>/`, så ingenting går tapt.

## Sikkerhetskopioppgaver

Du lager én eller flere oppgaver på forsiden. Hver oppgave har et navn, et innhold (alt, bare personlige filer, bare
oppsett og innstillinger – eller egne valg), et lagringssted og hvor ofte den kjøres (eller bare manuelt). Eksempel:
«Filer» til en ekstern disk hver time og «Innstillinger» til NAS-en hver dag. Oppgaver kan redigeres og slettes;
kopiene som allerede er tatt, blir liggende. Mens en kopi kjører, vises fremdriften i oppgavens rad og i en
statuslinje nederst i vinduet, og statusfeltet kan varsle når en kopi starter i bakgrunnen («Vis fremdrift»).

## Lagring: restic

Sikkerhetskopiene lagres i et [restic](https://restic.net)-lager. Første gang kopieres alt, deretter bare det som er
nytt eller endret (deduplisering). Hvert tidspunkt er likevel en hel kopi. Alt krypteres med AES-256 og passordet
ditt, som ligger i GNOME-nøkkelringen.

Lagringssteder: en mappe på denne maskinen, en USB-disk eller en NAS i nettverket. For en NAS logger du inn med
brukernavn og passord (SMB eller SFTP) og blar deg frem til mappen; Waypoint finner også NAS-er som annonserer seg
på nettverket. Tilkoblingen går via GNOMEs GVfs (som Filer), og passordet lagres i nøkkelringen, så de automatiske
kopiene kan koble til NAS-en selv. Lageret legges i mappen `Waypoint` på stedet du velger.

restic installeres fra pakkesystemet ved behov (`sudo zypper install restic`). Kopiene kan også leses uten Waypoint:
`restic -r <lager> snapshots` og `restic -r <lager> restore latest --target /tmp/x`.

## Automatisk, som Time Machine

En systemd-timer for brukeren (`waypoint-auto.timer`) kjører `waypoint auto` hvert 15. minutt. En ny kopi tas når
det har gått en time, et døgn eller en uke, og lagringsstedet er tilgjengelig. Regler: ikke på batteri, ikke på målt
nettverk (for NAS), og varsel når det er for lenge siden siste kopi. Gamle tidspunkter tynnes ut: alt fra siste
døgn, ett per dag den siste måneden og ett per uke før det; blir disken full, slettes de eldste. Kopiene kontrolleres
jevnlig (`restic check --read-data-subset 2%`).

Status ligger i `~/.local/state/waypoint/status.json`. Ikonet i statusfeltet (`waypoint tray`) viser siste og neste
kopi, blå prikk mens en kopi tas, gul når den venter og rød ved feil.

## Eksport til én fil

«Eksporter oppsett» lagrer oppsettet i én `tar.gz`-fil for å flytte det til en annen maskin. Nøkler og passord
krypteres alltid med gpg (AES-256), og hele filen kan krypteres (standard): `gpg -d fil.tar.gz.gpg | tar xz`.
Sikkerhetskopier fra Flytt (det gamle navnet) kan fortsatt gjenopprettes.

## Oppstart og oppdateringer

Innstillinger → Generelt: vis ikon i statusfeltet, start ved innlogging, se etter nye versjoner (én gang i døgnet,
fra GitHub) og eventuelt installer dem automatisk. Waypoint ligger i hjemmemappen, så oppdateringen trenger ikke
administratorpassord. Automatiske sikkerhetskopier går uansett, også når Waypoint ikke er startet.

## Språk og utseende

Norsk, engelsk, tysk, spansk og fransk; følger systemspråket eller velges i Innstillinger. Følger systemets lyse
eller mørke tema. Nye tekster: legg dem i `tools/translations.py` og kjør `python3 tools/make_locales.py`.

## Installere

```sh
./install.sh      # til ~/.local, uten sudo
./uninstall.sh
```

Trenger Python 3.10+, PyGObject, GTK 4.12+, libadwaita 1.6+ og restic (gpg for eksportfiler).

## Kommandolinje

```sh
waypoint                    # åpner vinduet
waypoint backup [--job ID]  # tar en sikkerhetskopi nå (én oppgave, eller alle)
waypoint jobs               # viser oppgavene
waypoint browse             # henter enkeltfiler fra et tidligere tidspunkt
waypoint snapshots          # viser tidspunktene på lagringsstedet
waypoint check              # kontrollerer sikkerhetskopiene
waypoint export [fil]       # eksporterer oppsettet til én fil
waypoint inspect <fil>      # viser innholdet i en eksportert fil
waypoint list               # viser hva som ville blitt tatt med
waypoint preferences        # åpner innstillingene
waypoint auto [--now]       # automatisk sikkerhetskopi (kjøres av timeren)
waypoint tray               # ikonet i statusfeltet
```

## Utvikling

```sh
python3 -m unittest discover -s tests       # restic-testen hoppes over hvis restic mangler
python3 -m waypoint                         # kjør fra kildekoden
WAYPOINT_SCREENSHOT=ut.png WAYPOINT_PAGE=backup WAYPOINT_SCREENSHOT_DELAY=4 python3 -m waypoint
```
