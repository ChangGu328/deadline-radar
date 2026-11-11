#!/usr/bin/env python3
"""deadline-radar: track assignment and project deadlines in a small local JSON file.

Standard library only. Usable as a command line tool and as an importable module.

Command line:

    deadline_radar.py add "Problem set 4" --due 2026-03-14 --course Algorithms --hours 6
    deadline_radar.py list
    deadline_radar.py due-soon --days 3
    deadline_radar.py done <id>
    deadline_radar.py remove <id>
    deadline_radar.py summary

Every command accepts --json (machine output) and --now (a fixed clock for tests).
Exit status: 0 on success, 2 on a user error, 1 when the store cannot be read or written
because of the operating system.

Titles, courses and notes are stored and printed as plain data. Nothing in this file
interprets them as commands.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

__version__ = "1.0.0"

STORE_VERSION = 1
ENV_FILE = "DEADLINE_RADAR_FILE"
ENV_TZ = "DEADLINE_RADAR_TZ"
ENV_NOW = "DEADLINE_RADAR_NOW"
MAX_TITLE_LENGTH = 200
MAX_COURSE_LENGTH = 80
MAX_NOTES_LENGTH = 2000
MAX_HOURS = 1000.0
MAX_DAYS = 365
DEFAULT_SOON_DAYS = 3
DEFAULT_PLAN_DAYS = 7
URGENCY_LEVELS = ("overdue", "today", "soon", "later")

_DATE_FORMAT = "%Y-%m-%d"
_DATETIME_FORMAT = "%Y-%m-%dT%H:%M"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,23}$")


def _has_control(text: str, *, allow_line_breaks: bool = False) -> bool:
    """True when ``text`` holds a control character (tabs and newlines optional)."""
    allowed = {9, 10} if allow_line_breaks else set()
    return any((ord(char) < 32 or ord(char) == 127) and ord(char) not in allowed for char in text)


class DeadlineError(Exception):
    """A user-facing error: bad input, an unknown id, or an unreadable store file."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Deadline:
    """One tracked deadline. ``due`` is ``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM`` local time."""

    id: str
    title: str
    due: str
    course: str = ""
    notes: str = ""
    hours: float | None = None
    status: str = "open"
    created: str = ""
    completed: str = ""

    @property
    def all_day(self) -> bool:
        return "T" not in self.due

    def due_at(self) -> datetime:
        """The moment the item is due. A date without a time means the end of that day."""
        if self.all_day:
            return datetime.strptime(self.due, _DATE_FORMAT).replace(hour=23, minute=59)
        return datetime.strptime(self.due, _DATETIME_FORMAT)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "due": self.due,
            "course": self.course,
            "notes": self.notes,
            "hours": self.hours,
            "status": self.status,
            "created": self.created,
            "completed": self.completed,
        }


# --------------------------------------------------------------------------- clock


def parse_due(text: str) -> str:
    """Validate a due date and return it in canonical form."""
    value = text.strip()
    try:
        if _DATE_RE.match(value):
            return datetime.strptime(value, _DATE_FORMAT).strftime(_DATE_FORMAT)
        if _DATETIME_RE.match(value):
            parsed = datetime.strptime(value.replace(" ", "T"), _DATETIME_FORMAT)
            return parsed.strftime(_DATETIME_FORMAT)
    except ValueError:
        pass
    raise DeadlineError(
        "invalid_input",
        f"invalid due date {text!r}: use YYYY-MM-DD or YYYY-MM-DDTHH:MM with a real calendar date",
    )


def parse_now(text: str) -> datetime:
    """Parse a clock override (``YYYY-MM-DD`` means midnight)."""
    value = text.strip()
    try:
        if _DATE_RE.match(value):
            return datetime.strptime(value, _DATE_FORMAT)
        if _DATETIME_RE.match(value):
            return datetime.strptime(value.replace(" ", "T"), _DATETIME_FORMAT)
    except ValueError:
        pass
    raise DeadlineError(
        "invalid_input", f"invalid --now value {text!r}: use YYYY-MM-DD or YYYY-MM-DDTHH:MM"
    )


def resolve_timezone() -> tuple[str, tzinfo]:
    """Timezone from DEADLINE_RADAR_TZ, else TZ, else UTC.

    An unknown DEADLINE_RADAR_TZ is a user error. An unusable TZ falls back to UTC, because
    TZ is often set by the system in a form that is not an IANA name.
    """
    explicit = (os.getenv(ENV_TZ) or "").strip()
    if explicit:
        if explicit.upper() == "UTC":
            return "UTC", UTC
        try:
            return explicit, ZoneInfo(explicit)
        except (KeyError, ValueError, OSError) as exc:
            raise DeadlineError(
                "invalid_input",
                f"unknown timezone {explicit!r} in {ENV_TZ}: use an IANA name such as "
                "America/Los_Angeles or UTC",
            ) from exc
    inherited = (os.getenv("TZ") or "").strip().lstrip(":")
    if inherited and inherited.upper() != "UTC":
        try:
            return inherited, ZoneInfo(inherited)
        except (KeyError, ValueError, OSError):
            pass
    return "UTC", UTC


def current_time(now_text: str | None = None) -> datetime:
    """The current local time as a naive datetime, to the minute.

    ``now_text`` (the --now option) wins, then DEADLINE_RADAR_NOW, then the real clock in
    the configured timezone.
    """
    override = now_text if now_text is not None else os.getenv(ENV_NOW)
    if override is not None and override.strip():
        return parse_now(override)
    _name, zone = resolve_timezone()
    return datetime.now(zone).replace(tzinfo=None, second=0, microsecond=0)


# --------------------------------------------------------------------------- store


def resolve_store_path() -> Path:
    """DEADLINE_RADAR_FILE, else $HERMES_HOME/deadline-radar/deadlines.json, else the
    same file under the .hermes directory in the user's home."""
    explicit = (os.getenv(ENV_FILE) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    hermes_home = (os.getenv("HERMES_HOME") or "").strip()
    base = Path(hermes_home).expanduser() if hermes_home else Path.home() / ".hermes"
    return base / "deadline-radar" / "deadlines.json"


def _corrupt(path: Path, detail: str) -> DeadlineError:
    return DeadlineError(
        "corrupt_store",
        f"the deadline store {path} is not valid ({detail}). It was left untouched; "
        f"fix or move the file, or point {ENV_FILE} at another file.",
    )


def _item_from_json(raw: Any, index: int, path: Path) -> Deadline:
    where = f"item {index}"
    if not isinstance(raw, dict):
        raise _corrupt(path, f"{where} is not an object")
    text_fields: dict[str, str] = {}
    for key in ("id", "title", "due", "course", "notes", "status", "created", "completed"):
        value = raw.get(key, "")
        if not isinstance(value, str):
            raise _corrupt(path, f"{where} field {key!r} is not text")
        text_fields[key] = value
    if not text_fields["id"] or not text_fields["title"]:
        raise _corrupt(path, f"{where} has no id or no title")
    try:
        due = parse_due(text_fields["due"])
    except DeadlineError as exc:
        raise _corrupt(path, f"{where} has a bad due date") from exc
    status = text_fields["status"] or "open"
    if status not in ("open", "done"):
        raise _corrupt(path, f"{where} has an unknown status")
    hours_raw = raw.get("hours")
    hours: float | None = None
    if hours_raw is not None:
        if isinstance(hours_raw, bool) or not isinstance(hours_raw, (int, float)):
            raise _corrupt(path, f"{where} field 'hours' is not a number")
        hours = float(hours_raw)
        if not 0 < hours <= MAX_HOURS:
            raise _corrupt(path, f"{where} field 'hours' is out of range")
    return Deadline(
        id=text_fields["id"],
        title=text_fields["title"],
        due=due,
        course=text_fields["course"],
        notes=text_fields["notes"],
        hours=hours,
        status=status,
        created=text_fields["created"],
        completed=text_fields["completed"],
    )


def load_store(path: Path) -> list[Deadline]:
    """Read and validate the store. A missing file is an empty store."""
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        return []
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _corrupt(path, "it is not JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise _corrupt(path, "it has no 'items' list")
    if data.get("version") != STORE_VERSION:
        raise _corrupt(path, f"its version is not {STORE_VERSION}")
    items = [_item_from_json(raw, index, path) for index, raw in enumerate(data["items"])]
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            raise _corrupt(path, f"id {item.id!r} appears twice")
        seen.add(item.id)
    return items


def save_store(path: Path, items: Sequence[Deadline]) -> None:
    """Write the store atomically: a temporary file in the same directory, then a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": STORE_VERSION, "items": [item.to_json() for item in items]}
    text = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    handle, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- views


def urgency_for(due_at: datetime, now: datetime, soon_days: int = DEFAULT_SOON_DAYS) -> str:
    """overdue (already past), today, soon (within ``soon_days`` days), or later."""
    if due_at < now:
        return "overdue"
    days = (due_at.date() - now.date()).days
    if days == 0:
        return "today"
    if days <= soon_days:
        return "soon"
    return "later"


def describe(item: Deadline, now: datetime, soon_days: int = DEFAULT_SOON_DAYS) -> dict[str, Any]:
    """A deadline plus its computed ``days_left`` and ``urgency``."""
    due_at = item.due_at()
    view = item.to_json()
    view["all_day"] = item.all_day
    view["days_left"] = (due_at.date() - now.date()).days
    view["urgency"] = urgency_for(due_at, now, soon_days) if item.status == "open" else "done"
    return view


def _sort_key(item: Deadline) -> tuple[datetime, str, str]:
    return (item.due_at(), item.title.lower(), item.id)


def _check_days(days: int, name: str = "--days") -> int:
    if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= MAX_DAYS:
        raise DeadlineError("invalid_input", f"{name} must be a whole number from 0 to {MAX_DAYS}")
    return days


def _clean_title(title: str) -> str:
    value = title.strip()
    if not value:
        raise DeadlineError("invalid_input", "the title must not be empty")
    if _has_control(value):
        raise DeadlineError("invalid_input", "the title must be a single line of plain text")
    if len(value) > MAX_TITLE_LENGTH:
        raise DeadlineError(
            "invalid_input",
            f"the title is {len(value)} characters; the limit is {MAX_TITLE_LENGTH}",
        )
    return value


def _clean_hours(hours: float | None) -> float | None:
    if hours is None:
        return None
    if isinstance(hours, bool) or not isinstance(hours, (int, float)):
        raise DeadlineError("invalid_input", "estimated hours must be a number")
    value = float(hours)
    if not 0 < value <= MAX_HOURS:  # also rejects NaN
        raise DeadlineError(
            "invalid_input", f"estimated hours must be above 0 and at most {MAX_HOURS:g}"
        )
    return round(value, 2)


def _derive_id(title: str, due: str, taken: dict[str, Deadline]) -> str:
    """A short id that is stable for a given title and due date."""
    digest = hashlib.sha256(f"{title.lower()}\n{due}".encode()).hexdigest()
    for length in range(6, 25, 2):
        candidate = digest[:length]
        other = taken.get(candidate)
        if other is None:
            return candidate
        if other.title.lower() == title.lower() and other.due == due:
            raise DeadlineError(
                "duplicate",
                f"duplicate: {title!r} due {due} is already tracked with id {other.id}",
            )
    raise DeadlineError("duplicate", "could not derive a free id; pass --id explicitly")


def add_deadline(
    path: Path,
    title: str,
    due: str,
    *,
    course: str = "",
    notes: str = "",
    hours: float | None = None,
    item_id: str | None = None,
    now: datetime | None = None,
) -> Deadline:
    """Validate and store a new open deadline. Returns the stored item."""
    clean_title = _clean_title(title)
    clean_due = parse_due(due)
    clean_course = course.strip()
    if _has_control(clean_course) or len(clean_course) > MAX_COURSE_LENGTH:
        raise DeadlineError(
            "invalid_input",
            f"the course label must be one line of at most {MAX_COURSE_LENGTH} characters",
        )
    clean_notes = notes.strip()
    if _has_control(clean_notes, allow_line_breaks=True) or len(clean_notes) > MAX_NOTES_LENGTH:
        raise DeadlineError(
            "invalid_input", f"notes must be plain text of at most {MAX_NOTES_LENGTH} characters"
        )
    clean_hours = _clean_hours(hours)
    items = load_store(path)
    taken = {item.id: item for item in items}
    if item_id is not None and item_id.strip():
        new_id = item_id.strip().lower()
        if not _ID_RE.match(new_id):
            raise DeadlineError(
                "invalid_input",
                "an id must be 2 to 24 characters: lowercase letters, digits and hyphens, "
                "starting with a letter or digit",
            )
        if new_id in taken:
            raise DeadlineError("duplicate", f"duplicate id {new_id!r}: it is already in use")
    else:
        new_id = _derive_id(clean_title, clean_due, taken)
    moment = now if now is not None else current_time()
    item = Deadline(
        id=new_id,
        title=clean_title,
        due=clean_due,
        course=clean_course,
        notes=clean_notes,
        hours=clean_hours,
        status="open",
        created=moment.strftime(_DATETIME_FORMAT),
    )
    save_store(path, [*items, item])
    return item


def list_deadlines(
    path: Path,
    now: datetime,
    *,
    include_done: bool = False,
    soon_days: int = DEFAULT_SOON_DAYS,
) -> list[dict[str, Any]]:
    """Open items sorted by due date (soonest first); done items too when asked."""
    _check_days(soon_days)
    items = [item for item in load_store(path) if include_done or item.status == "open"]
    return [describe(item, now, soon_days) for item in sorted(items, key=_sort_key)]


def due_soon(path: Path, now: datetime, days: int = DEFAULT_SOON_DAYS) -> list[dict[str, Any]]:
    """Open items that are overdue or due within ``days`` days, soonest first."""
    views = list_deadlines(path, now, soon_days=_check_days(days))
    return [view for view in views if view["urgency"] != "later"]


def _find(items: Sequence[Deadline], item_id: str) -> int:
    wanted = item_id.strip().lower()
    if not wanted:
        raise DeadlineError("invalid_input", "an id is required")
    for index, item in enumerate(items):
        if item.id == wanted:
            return index
    raise DeadlineError("not_found", f"no deadline has id {wanted!r}; run 'list' to see the ids")


def mark_done(path: Path, item_id: str, now: datetime | None = None) -> Deadline:
    """Mark one open item as done. Returns the updated item."""
    items = load_store(path)
    index = _find(items, item_id)
    current = items[index]
    if current.status == "done":
        raise DeadlineError("invalid_input", f"deadline {current.id!r} is already done")
    moment = now if now is not None else current_time()
    updated = Deadline(
        id=current.id,
        title=current.title,
        due=current.due,
        course=current.course,
        notes=current.notes,
        hours=current.hours,
        status="done",
        created=current.created,
        completed=moment.strftime(_DATETIME_FORMAT),
    )
    items[index] = updated
    save_store(path, items)
    return updated


def remove_deadline(path: Path, item_id: str) -> Deadline:
    """Delete one item, open or done. Returns the removed item."""
    items = load_store(path)
    index = _find(items, item_id)
    removed = items.pop(index)
    save_store(path, items)
    return removed


# --------------------------------------------------------------------------- summary


def _work_days(item: Deadline, now: datetime) -> list[date]:
    """The days on which work for ``item`` can still happen, starting today.

    A date-only item can be worked on through its due day. An item with a due time is
    planned to finish the day before, unless it is due today or already late.
    """
    today = now.date()
    due_at = item.due_at()
    last = due_at.date()
    if not item.all_day and last > today:
        last -= timedelta(days=1)
    if due_at < now or last <= today:
        return [today]
    return [today + timedelta(days=offset) for offset in range((last - today).days + 1)]


def build_plan(
    items: Sequence[Deadline], now: datetime, plan_days: int = DEFAULT_PLAN_DAYS
) -> list[dict[str, Any]]:
    """Spread each open item's estimated hours evenly over its remaining days.

    Returns one entry per day for the next ``plan_days`` days that has work in it.
    """
    _check_days(plan_days, "--plan-days")
    today = now.date()
    buckets: dict[date, list[dict[str, Any]]] = {}
    for item in sorted(items, key=_sort_key):
        if item.status != "open" or item.hours is None:
            continue
        days = _work_days(item, now)
        share = item.hours / len(days)
        for day in days:
            buckets.setdefault(day, []).append(
                {"id": item.id, "title": item.title, "hours": round(share, 2)}
            )
    plan: list[dict[str, Any]] = []
    for day in sorted(buckets):
        if (day - today).days >= max(plan_days, 1):
            break
        entries = buckets[day]
        plan.append(
            {
                "date": day.strftime(_DATE_FORMAT),
                "total_hours": round(sum(entry["hours"] for entry in entries), 2),
                "items": entries,
            }
        )
    return plan


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _relative(view: dict[str, Any]) -> str:
    days = int(view["days_left"])
    if view["urgency"] == "overdue":
        return "overdue since earlier today" if days == 0 else f"{_plural(-days, 'day')} overdue"
    if days == 0:
        return "due today"
    return f"in {_plural(days, 'day')}"


def _label(view: dict[str, Any]) -> str:
    title = str(view["title"])
    return f'"{title}" ({view["course"]})' if view["course"] else f'"{title}"'


def summarize(
    path: Path,
    now: datetime,
    *,
    days: int = DEFAULT_SOON_DAYS,
    plan_days: int = DEFAULT_PLAN_DAYS,
) -> dict[str, Any]:
    """A short paragraph for a morning briefing plus a suggested daily work plan."""
    _check_days(days)
    items = [item for item in load_store(path) if item.status == "open"]
    views = [describe(item, now, days) for item in sorted(items, key=_sort_key)]
    counts = {
        level: sum(1 for view in views if view["urgency"] == level) for level in URGENCY_LEVELS
    }
    total_hours = round(sum(item.hours or 0.0 for item in items), 2)
    plan = build_plan(items, now, plan_days)
    stamp = now.strftime("%a %Y-%m-%d %H:%M")
    if not views:
        text = f"As of {stamp} there are no open deadlines."
    else:
        parts = []
        if counts["overdue"]:
            parts.append(f"{counts['overdue']} overdue")
        if counts["today"]:
            parts.append(f"{counts['today']} due today")
        if counts["soon"]:
            parts.append(f"{counts['soon']} due within {_plural(days, 'day')}")
        if counts["later"]:
            parts.append(f"{counts['later']} later")
        first = views[0]
        text = (
            f"As of {stamp} you have {_plural(len(views), 'open deadline')}: "
            f"{', '.join(parts)}. Most urgent: {_label(first)}, due "
            f"{str(first['due']).replace('T', ' ')} ({_relative(first)})."
        )
        if total_hours:
            text += f" Estimated work remaining: {total_hours:g} hours."
        if plan and plan[0]["date"] == now.strftime(_DATE_FORMAT):
            text += f" Suggested for today: {plan[0]['total_hours']:g} hours."
    return {
        "text": text,
        "counts": {"open": len(views), **counts},
        "total_hours": total_hours,
        "next": views[0] if views else None,
        "plan": plan,
    }


# --------------------------------------------------------------------------- CLI


def _format_line(view: dict[str, Any]) -> str:
    extras = [_relative(view)] if view["urgency"] != "done" else ["done"]
    if view["hours"] is not None:
        extras.append(f"~{view['hours']:g}h")
    course = f" ({view['course']})" if view["course"] else ""
    due = str(view["due"]).replace("T", " ")
    return (
        f"[{view['urgency']:<7}] {view['id']:<8} {due:<16} {view['title']}{course}"
        f" - {', '.join(extras)}"
    )


def _format_plan(plan: Sequence[dict[str, Any]]) -> list[str]:
    lines = []
    for day in plan:
        parts = "; ".join(f"{entry['title']} {entry['hours']:g}h" for entry in day["items"])
        lines.append(f"  {day['date']}: {day['total_hours']:g}h - {parts}")
    return lines


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print machine-readable JSON")
    common.add_argument(
        "--now", metavar="WHEN", help="fixed clock for tests: YYYY-MM-DD or YYYY-MM-DDTHH:MM"
    )
    parser = argparse.ArgumentParser(
        prog="deadline_radar.py",
        description="Track assignment and project deadlines in a local JSON file.",
    )
    parser.add_argument("--version", action="version", version=f"deadline-radar {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    add = commands.add_parser("add", parents=[common], help="add a deadline")
    add.add_argument("title", help="what is due (at most 200 characters)")
    add.add_argument("--due", required=True, help="YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    add.add_argument("--course", "--label", dest="course", default="", help="course or label")
    add.add_argument("--notes", default="", help="free-text notes")
    add.add_argument("--hours", type=float, default=None, help="estimated hours of work")
    add.add_argument("--id", dest="item_id", default=None, help="choose the id yourself")

    listing = commands.add_parser("list", parents=[common], help="list open deadlines by due date")
    listing.add_argument("--all", action="store_true", help="include finished items")

    soon = commands.add_parser(
        "due-soon", parents=[common], help="overdue items and items due within N days"
    )
    soon.add_argument("--days", type=int, default=DEFAULT_SOON_DAYS, help="window in days")

    done = commands.add_parser("done", parents=[common], help="mark a deadline as done")
    done.add_argument("id", help="the id shown by list")

    remove = commands.add_parser("remove", parents=[common], help="delete a deadline")
    remove.add_argument("id", help="the id shown by list")

    summary = commands.add_parser(
        "summary", parents=[common], help="briefing paragraph and a daily work plan"
    )
    summary.add_argument("--days", type=int, default=DEFAULT_SOON_DAYS, help="what counts as soon")
    summary.add_argument(
        "--plan-days", type=int, default=DEFAULT_PLAN_DAYS, help="days of work plan to show"
    )
    return parser


def _run(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    """Execute one command. Returns the JSON payload and the human-readable lines."""
    now = current_time(args.now)
    path = resolve_store_path()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    if args.command == "add":
        item = add_deadline(
            path,
            args.title,
            args.due,
            course=args.course,
            notes=args.notes,
            hours=args.hours,
            item_id=args.item_id,
            now=now,
        )
        view = describe(item, now)
        return {"item": view}, [f"Added {view['id']}: {_format_line(view)}"]
    if args.command == "list":
        views = list_deadlines(path, now, include_done=args.all)
        head = f"{_plural(len(views), 'deadline')} as of {stamp}"
        return {"count": len(views), "items": views}, [head, *map(_format_line, views)]
    if args.command == "due-soon":
        views = due_soon(path, now, args.days)
        head = (
            f"{_plural(len(views), 'deadline')} overdue or due within "
            f"{_plural(args.days, 'day')} as of {stamp}"
        )
        payload = {"count": len(views), "days": args.days, "items": views}
        return payload, [head, *map(_format_line, views)]
    if args.command == "done":
        view = describe(mark_done(path, args.id, now), now)
        return {"item": view}, [f"Done {view['id']}: {view['title']}"]
    if args.command == "remove":
        view = describe(remove_deadline(path, args.id), now)
        return {"item": view}, [f"Removed {view['id']}: {view['title']}"]
    result = summarize(path, now, days=args.days, plan_days=args.plan_days)
    lines = [str(result["text"])]
    if result["plan"]:
        lines.append("Suggested daily plan:")
        lines.extend(_format_plan(result["plan"]))
    return result, lines


def main(argv: Sequence[str] | None = None) -> int:
    """Command line entry point. Returns the process exit status."""
    args = _build_parser().parse_args(argv)
    try:
        payload, lines = _run(args)
    except DeadlineError as exc:
        if args.json:
            error = {"code": exc.code, "message": exc.message}
            print(json.dumps({"ok": False, "error": error}, ensure_ascii=True, sort_keys=True))
        print(f"error: {exc.message}", file=sys.stderr)
        return 2
    except OSError as exc:
        message = f"cannot use the deadline store: {exc.strerror or exc.__class__.__name__}"
        if args.json:
            error = {"code": "store_unavailable", "message": message}
            print(json.dumps({"ok": False, "error": error}, ensure_ascii=True, sort_keys=True))
        print(f"error: {message}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"ok": True, **payload}, ensure_ascii=True, sort_keys=True))
    else:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
