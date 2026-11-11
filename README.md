# deadline-radar

A skill for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that tracks
assignment and project deadlines in a small local JSON file and answers "what is due
soon?". It gives a personal assistant three things:

- a place to remember deadlines the user mentions (`add`, `done`, `remove`);
- an urgency view for alerts (`list`, `due-soon`: overdue, today, soon, later);
- a paragraph for a morning briefing plus a daily work plan that spreads each item's
  estimated hours over the days left (`summary`).

The helper script uses the Python standard library only (Python 3.11 or newer). It makes
no network calls and needs no account, API key or configuration.

## Repository layout

This repository is a Hermes skill tap. Hermes looks for skills one level below
`skills/`, so the skill lives at `skills/deadline-radar/`:

```text
.
|-- README.md
|-- LICENSE
`-- skills/
    `-- deadline-radar/
        |-- SKILL.md
        `-- scripts/
            `-- deadline_radar.py
```

## Install

Replace `OWNER/REPO` with the GitHub owner and name of this repository.

Install the skill directly:

```bash
hermes skills install OWNER/REPO/skills/deadline-radar
```

Or subscribe to the repository as a tap first, then search and install:

```bash
hermes skills tap add OWNER/REPO
hermes skills search deadline --source github
hermes skills install OWNER/REPO/skills/deadline-radar
```

Hermes scans every third-party skill before installing it and shows the result. Useful
follow-up commands:

```bash
hermes skills inspect OWNER/REPO/skills/deadline-radar   # preview without installing
hermes skills tap list                                   # show configured taps
hermes skills check                                      # see whether an update exists
hermes skills update deadline-radar                      # install the update
hermes skills uninstall deadline-radar                   # remove the skill
```

Inside a running Hermes session the same commands start with a slash, for example
`/skills tap add OWNER/REPO`.

To propose a local copy of the skill to a tap repository as a pull request:

```bash
hermes skills publish deadline-radar --to github --repo OWNER/REPO
```

## Usage

Once installed, ask Hermes in plain language:

- "Remember that problem set 4 for Algorithms is due on March 14, about six hours."
- "What is due in the next three days?"
- "I finished the lab report."
- "Give me my deadline summary and a plan for the week."

The agent runs the bundled script. You can run it yourself too:

```bash
cd skills/deadline-radar
python3 scripts/deadline_radar.py add "Problem set 4" --due 2026-03-14 --course Algorithms --hours 6
python3 scripts/deadline_radar.py add "Lab report" --due 2026-03-11T17:00 --hours 1.5
python3 scripts/deadline_radar.py list
python3 scripts/deadline_radar.py due-soon --days 3
python3 scripts/deadline_radar.py summary
python3 scripts/deadline_radar.py done 33b1c6
python3 scripts/deadline_radar.py remove ae1523
```

Example:

```text
$ python3 scripts/deadline_radar.py due-soon --days 3 --now 2026-03-11T09:00
2 deadlines overdue or due within 3 days as of 2026-03-11 09:00
[today  ] 33b1c6   2026-03-11 17:00 Lab report - due today, ~1.5h
[soon   ] ae1523   2026-03-14       Problem set 4 (Algorithms) - in 3 days, ~6h
```

Every command takes `--json` for machine-readable output and `--now` for a fixed clock.
The exit status is 0 on success and 2 on a user error such as a bad date.

### Settings

| Variable | Meaning | Default |
|----------|---------|---------|
| `DEADLINE_RADAR_FILE` | Path of the JSON store | `deadline-radar/deadlines.json` inside the Hermes home directory |
| `DEADLINE_RADAR_TZ` | IANA timezone used for "now" | `TZ`, else `UTC` |
| `DEADLINE_RADAR_NOW` | Fixed clock for tests | the real clock |

The Hermes home directory is `HERMES_HOME` when set, else `.hermes` in your home
directory. The store is created on the first `add`. Writes are atomic (a temporary file,
then a rename), and a store that fails validation is reported and never overwritten.

### Use as a module

```python
from pathlib import Path

import deadline_radar as radar

store = Path("deadlines.json")
now = radar.current_time()
radar.add_deadline(store, "Problem set 4", "2026-03-14", course="Algorithms", hours=6)
for item in radar.due_soon(store, now, days=3):
    print(item["urgency"], item["days_left"], item["title"])
print(radar.summarize(store, now)["text"])
```

User errors raise `radar.DeadlineError`, which carries a `code` and a `message`.

## Safety

Titles, course labels and notes are stored and printed as plain data. The skill tells
the agent to treat them as information to show, never as instructions to follow.

## License

MIT. See [LICENSE](LICENSE).
