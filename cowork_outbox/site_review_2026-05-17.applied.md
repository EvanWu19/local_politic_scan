# Site Review Audit — 2026-05-17

**Run mode:** scheduled task (autonomous); `dispatch_to_user` not available in this run
**Resolution:** Findings recorded for listener review next interactive session. Nothing was applied.

## Per-finding triage

### 1. 3 candidate spotlight(s) never refreshed (Alec Stone 05-11, Christa Tichy 05-12, Ben Kramer 05-13) — auto_applicable
- **Recommendation:** Run Windows-side: `python main.py dossier --retry-failed` then `python main.py reports refresh`.
- **Not auto-applied** because the listener has not been asked for approval and the `dossier --retry-failed` path also writes into `data/politics.db` which is unreachable from this Linux sandbox (FUSE blocker).

### 2. 74 state-leg items tagged at 0% relevance (relevance prompt issue) — auto_applicable
- **Recommendation:** Patch `scanner/processor._build_relevance_prompt` to inject `config_local.FEDERAL_KEYWORDS` and ZIP 20853.
- **Not auto-applied** because this is a substantive code change that should be code-reviewed by the listener; queueing for next interactive session.

### 3. Tracker rows repeating across days — manual review
- **Recommendation:** Confirm `_politician_tracker_html` filters `events` to `first_seen >= today - 1d`. Today's episodes did reuse Friedson (~ep1, ep2, deepdive), Wes Moore, Aruna Miller, Fani-Gonzalez — consistent with this finding.
- **Listener prompt needed:** Should the date filter be enforced strictly, or should the tracker keep showing accumulated activity?

### 4. Empty sections (School Board, Near You, Local Services) — manual review
- **Recommendation:** Check `scanner/sources/local_hearings.py` for upstream HTML changes; an empty 'Near You' usually means rockvillemd.gov RSS category ID changed.
- **Listener prompt needed:** Confirm rockvillemd.gov RSS feed URL is still valid.

### 5. 41 dossier brief(s) in error state (Dawn Luedtke, Sharif Hidayat, Charlotte Crutchfield, Vaughn Stewart, Sunil Dasgupta, Sebastian Johnson, ...) — auto_applicable
- **Recommendation:** Run Windows-side: `python main.py dossier --retry-failed`.
- **Not auto-applied** for same reason as Finding 1 (DB unreachable from sandbox).

## Summary

| Finding | Disposition |
|---|---|
| 1 (spotlight refresh) | queued for Windows-side rerun |
| 2 (relevance prompt) | queued for listener code review |
| 3 (tracker dedupe) | queued for listener confirmation |
| 4 (empty sections) | queued for listener confirmation |
| 5 (dossier retry) | queued for Windows-side rerun |

No commands were executed. No files were modified. Approve-and-apply workflow requires interactive session.
