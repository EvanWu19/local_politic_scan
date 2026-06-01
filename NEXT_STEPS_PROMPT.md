# Prompt for Claude Code

Copy everything between the lines below and paste it into Claude Code from
inside `C:\Users\evan_\local_politic_scan`.

---

I've just received a batch of patches from Cowork on the local-politics scanner. The patches:

- Hard-pin `USE_COWORK_FOR_AI = True` in `config.py`
- Retire the direct-Anthropic-API fallback in `scanner/processor.py`, `scanner/analyst.py`, `scanner/pm.py`, `scanner/editor.py`, `scanner/chat.py`, `scanner/podcast.py`, `scanner/server.py`, `scanner/deepdive.py`
- Add `scanner/notifications.py` (surfaces failures via log + SQLite + a daily rollup the drain task dispatches)
- Add `scanner/sources/local_hearings.py` (Rockville City, Rockville Planning, MCPS BoardDocs, M-NCPPC, WSSC)
- Add `weekly_review.py` and the `--retry-failed` flag on `dossier`
- Bump default AI model to `claude-opus-4-7` (with env-var override)
- Patch `scanner/reporter.py`: spotlight fallback shows real status, tracker filters to last 24h, references split into "New today" / "Seen earlier this week"
- Patch `scanner/database.py`: new `digest_references` table + helpers, `recent_events_for_politician()`

Please run the following checklist and report back. Do NOT skip the verification steps — they prevent silent failures.

## 1. Verify the patches parse and the modules import

Run these and paste the output:

```powershell
python -c "import config; print('config OK, USE_COWORK_FOR_AI =', config.Config.USE_COWORK_FOR_AI)"
python -c "from scanner import notifications, reporter, dossier, database, processor, analyst, pm, editor, chat, podcast, server, deepdive; print('all scanner modules import OK')"
python -c "from scanner.sources import local_hearings; print('local_hearings OK')"
python -c "import weekly_review; print('weekly_review OK')"
```

If any of those fail, fix the import / syntax error before continuing. The most likely culprit is a model string `claude-opus-4-7` if my Anthropic account doesn't have Opus 4.7 yet — in that case set these in `.env` and re-test:

```
PROCESSOR_MODEL=claude-opus-4-6
ANALYST_MODEL=claude-opus-4-6
PODCAST_SCRIPT_MODEL=claude-opus-4-6
CHAT_MODEL=claude-opus-4-6
COWORK_DOSSIER_MODEL=claude-opus-4-6
```

(Yes — keep `4-6` as the fallback, since my account is known to have it.)

## 2. Clear the stuck dossier briefs

Run:

```powershell
python main.py dossier --retry-failed
```

Expected: it should requeue every `.error.json` brief from the last 14 days under `cowork_inbox/`. Tell me how many were requeued and which candidates.

## 3. Re-render yesterday's digest with the spotlight + tracker + references fixes

Run:

```powershell
python main.py report --date 2026-05-14
```

Then open `reports/digest_2026-05-14.html` and tell me:
- Does the "Today's Candidate Spotlight" section now show a real status line (e.g. "Last attempt failed …" or "Recent activity on file…") instead of the "Dossier in progress" placeholder?
- Does the "References" section have a "🆕 New today" header and a collapsible "Seen earlier this week" group?
- Does the "Politician Tracker" section have "Quiet today — last activity …" labels for politicians with nothing new in 24h?

## 4. Pull the new local sources

Run:

```powershell
python main.py fetch
```

Then look at `scan.log` and tell me whether the `[3.5/6] Rockville / MCPS / Park & Planning / WSSC…` step ran and what counts it produced. If any of the five fetchers returned 0 items, the upstream HTML likely changed — open `scanner/sources/local_hearings.py` and check the URLs in `ROCKVILLE_RSS`, `MCPS_LISTING`, `MNCPPC_AGENDA`, `WSSC_MEETINGS` against the live sites, then patch the selectors.

## 5. Generate tomorrow's digest

Run:

```powershell
python main.py publish
```

Then verify a fresh `reports/digest_<today>.html` exists and the new `📍 Near You — Rockville / 20853 Hearings` section appears with real items.

## 6. Register the three new scheduled tasks

The improvement plan recommends three Cowork-side scheduled tasks. Register them via the `schedule` skill (run each as a separate `/schedule` invocation):

```
/schedule weekly-site-review
  cron: "0 8 * * SUN"
  command: "python weekly_review.py"
  label: "Local-politics weekly site review"

/schedule nightly-error-scan
  cron: "30 23 * * *"
  command: "python main.py notifications --scan"
  label: "Local-politics nightly error scan"

/schedule dossier-retry-failed
  cron: "45 23 * * *"
  command: "python main.py dossier --retry-failed"
  label: "Local-politics nightly dossier retry"
```

If the `schedule` skill isn't available in this environment, use Windows Task Scheduler via `schtasks /create` instead. Report which path you used.

## 7. Run a one-off weekly review now, dry-run

To prove the weekly review works end-to-end without dispatching anything:

```powershell
python main.py weekly-review --dry-run
```

Show me the report it printed.

## 8. Final summary

When everything above is done, tell me:
- Any step that produced an error and what you did about it
- Counts: dossiers requeued, local-hearing items fetched, digest items in the new sections
- Which model string ended up actually in use (Opus 4.7 vs 4.6 fallback)
- The three scheduled tasks' status

Be terse — under 200 words for the summary, just the facts.

---

End of prompt.
