# Five-person first-run trial

Recruitment is voluntary via [issue #73](https://github.com/Danish-Sethi/blazecrawl/issues/73).
Do not contact people without consent. No participant results have been collected here.

## Prerequisites and task

Recruit five external people who have not contributed to BlazeCrawl. Record only
anonymous IDs P1-P5 and OS/architecture, Docker experience and version tried.
Do not collect API keys, private URLs, names or raw terminal recordings. Participation
is optional; people may stop at any point. Ask permission before taking notes.

Before the clock: confirm Docker is installed/running with Linux containers, public
internet access, a terminal, and curl 7.71+ / Python 3 for the Bash path (PowerShell
needs neither). Prerequisite installation time is recorded separately, not hidden.
Use a confirmed publicly available pinned release, not a source checkout or `latest`.

Read this neutral prompt: **"Using the README, start BlazeCrawl and show the Markdown
for https://example.com. Tell us what is unclear. You may stop whenever you want."**
Do not demonstrate the commands or explain what to click/type in advance.

**Start:** participant first opens Quickstart after prerequisites are confirmed.
**End:** non-empty `data.markdown` from https://example.com is visible from an authenticated
static scrape, or 10 minutes elapse, or the participant stops. The recommended request
uses `only_main_content: false` to retain the heading; extraction of main content alone
may omit that heading and still be a successful result. Setup/auth errors count
within those 10 minutes. Observer hints are recorded as assistance, not independent success.

## Observation and debrief

Record elapsed time, independent/assisted success, exact failing step and redacted
error, number of hints, and the participant's own wording about confusion. Avoid
leading questions: "What did you expect?", "What slowed you down?", "What would you
change?" Do not ask "Was that easy?" Recheck the same failing step after a docs fix.

Acceptance target: **at least 4/5 independent successes within 10 minutes**.
Report the actual denominator, assistance and prerequisite failures. Five participants
are a qualitative trial, not a statistically representative performance claim.

## Empty results template

Release/image digest: __________  README commit: __________  Trial date: __________

| ID | OS / arch | Docker familiarity | Prereq time | First-result time | Independent / assisted / stopped / timeout | Failing step / redacted error | Hints |
|---|---|---|---|---|---|---|---|
| P1 | | | | | | | |
| P2 | | | | | | | |
| P3 | | | | | | | |
| P4 | | | | | | | |
| P5 | | | | | | | |

Independent successes / attempted: ____ / ____  
Recurring confusion (participant wording): __________  
Docs fix and observed retest result: __________

## Invitation text

"Would you volunteer for a 10-minute first-run trial of BlazeCrawl's README?
You need Docker running Linux containers and internet access. We want feedback on
the instructions, not a test of your skills. Participation is optional, you may
stop at any time, and we only record anonymous timings and redacted setup errors.
Please do not share API keys. Details and consent are in issue #73."
