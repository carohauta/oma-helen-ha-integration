# Triage Labels

The skills speak in terms of five canonical triage roles. This repo uses the default
vocabulary, so each role's label string is identical to its name.

Issues are local markdown files (see `issue-tracker.md`), so a label is recorded as a
`Status:` line near the top of the issue file rather than applied through a tracker API.

| Role              | `Status:` value   | Meaning                                  |
| ----------------- | ----------------- | ---------------------------------------- |
| `needs-triage`    | `needs-triage`    | Maintainer needs to evaluate this issue  |
| `needs-info`      | `needs-info`      | Waiting on reporter for more information |
| `ready-for-agent` | `ready-for-agent` | Fully specified, ready for an AFK agent  |
| `ready-for-human` | `ready-for-human` | Requires human implementation            |
| `wontfix`         | `wontfix`         | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), write the
corresponding `Status:` value from this table.
