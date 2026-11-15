"""Felles datamodell: hva som kan tas med, gruppert slik brukeren ser det."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Item:
    id: str                 # unik innenfor sin gruppe
    label: str
    detail: str = ""
    size: int = 0
    selected: bool = True
    available: bool = True  # False: vises, men kan ikke velges (f.eks. «allerede installert»)
    sensitive: bool = False  # private data som nøkler og passord – krypteres alltid
    default: bool | None = None  # standardvalget, så bare brukerens avvik lagres
    data: dict = field(default_factory=dict)


@dataclass
class Group:
    id: str                 # settings, wallpaper, extensions, repos, packages, flatpaks, files-<kategori>
    title: str
    subtitle: str = ""
    icon: str = "folder-symbolic"
    items: list[Item] = field(default_factory=list)
    flat: bool = False      # vis elementene direkte i stedet for i en utvidbar rad
    note: str = ""          # forklaring som vises når gruppen ikke kan brukes
    data: dict = field(default_factory=dict)

    def selected(self) -> list[Item]:
        return [i for i in self.items if i.selected and i.available]


@dataclass
class Section:
    title: str
    description: str
    groups: list[Group] = field(default_factory=list)


PRESETS = ("all", "files", "setup")


def apply_preset(sections: list[Section], preset: str):
    """Grunnvalget for en oppgave: «all» (standardvalgene), «files» (bare personlige mapper) eller «setup»
    (alt unntatt personlige mapper). Blir det nye standardvalget, så brukerens egne valg lagres som avvik fra det."""
    for s in sections:
        for g in s.groups:
            for i in g.items:
                if preset == "files":
                    i.selected = g.id == "files-personal" and i.available
                elif preset == "setup" and g.id == "files-personal":
                    i.selected = False
                i.default = i.selected


def apply_job(sections: list[Section], job: dict):
    apply_preset(sections, job.get("preset") or "all")
    apply_choices(sections, job.get("choices") or {})


def apply_choices(sections: list[Section], choices: dict):
    """Legger brukerens lagrede valg oppå standardvalgene."""
    for s in sections:
        for g in s.groups:
            for i in g.items:
                if i.default is None:
                    i.default = i.selected
                v = choices.get(g.id, {}).get(i.id)
                if v is not None and i.available:
                    i.selected = bool(v)


def collect_choices(sections: list[Section]) -> dict:
    out: dict = {}
    for s in sections:
        for g in s.groups:
            for i in g.items:
                if i.default is not None and i.available and i.selected != i.default:
                    out.setdefault(g.id, {})[i.id] = i.selected
    return out


def find_group(sections: list[Section], gid: str) -> Group | None:
    for s in sections:
        for g in s.groups:
            if g.id == gid:
                return g
    return None
