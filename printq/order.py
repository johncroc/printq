"""Which queued job a printer takes next, and whether the loaded spool suits it.

Changing a spool costs the operator a trip to the printer, so the queue does
not run strictly in order of submission: for each printer it first takes
every job the loaded spool can print, oldest first, and only then asks for
another spool, choosing the filament whose oldest job has waited longest.
Jobs that need the same filament therefore run back to back.

This module is the one place that decides the order. A manual arrangement
(dragging jobs on a board) belongs here too, ahead of the automatic rule.
"""
from __future__ import annotations


def needs(job: dict) -> tuple:
    """What a job asks of the spool: (Spoolman filament id or None, MATERIAL or '')."""
    return ((job.get("filament") or {}).get("id"), (job.get("material") or "").strip().upper())


def suits(job: dict, spool) -> bool:
    """True when the loaded spool can print the job. `spool` is moonraker.spool()'s dict; anything else
    (no spool tracking, nothing loaded) suits only a job that asks for nothing."""
    fid, material = needs(job)
    if fid is None and not material:
        return True
    if not isinstance(spool, dict):
        return False
    if fid is not None:
        return spool.get("filament_id") == fid
    return (spool.get("material") or "").strip().upper() == material


def describe(job: dict) -> str:
    fid, material = needs(job)
    return (job.get("filament") or {}).get("label") or material or "any filament"


def arrange(queued: list[dict], spool, last: dict | None = None) -> list[dict]:
    """The printer's queued jobs (given oldest first) in the order they should run.

    `spool` is what is loaded now. Where that is not known, `last` (the job that printed most recently on
    this printer) stands in for it: what it needed is taken to be still loaded."""
    def loaded(job: dict) -> bool:
        if isinstance(spool, dict):
            return suits(job, spool)
        return needs(job) == (None, "") or (last is not None and needs(job) == needs(last))

    first = [j for j in queued if loaded(j)]
    rest = [j for j in queued if not loaded(j)]
    groups: dict[tuple, list[dict]] = {}
    for job in rest:  # dicts keep insertion order: a group stands where its oldest job stood
        groups.setdefault(needs(job), []).append(job)
    return first + [job for group in groups.values() for job in group]


def spool_changes(ordered: list[dict]) -> int:
    """How many times the filament changes while this list runs in order."""
    wants = [needs(j) for j in ordered if needs(j) != (None, "")]
    return sum(1 for a, b in zip(wants, wants[1:]) if a != b)
