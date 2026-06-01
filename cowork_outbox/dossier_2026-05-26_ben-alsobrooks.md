# Ben Alsobrooks — Candidate Dossier (REGISTRY ERROR — NAME CONFUSION)

**REGISTRY CORRECTION NOTE (2026-05-27 drain):** No politician named "Ben Alsobrooks" surfaces in Maryland 2026 candidate filings. This entry appears to be a name-conflation error between two real people:

1. **Angela Alsobrooks** — Maryland's junior U.S. Senator (Democratic), former Prince George's County Executive. Elected to the Senate in November 2024. Not on the 2026 primary ballot (her seat is not up until 2030).
2. **Ben (Benjamin) Cardin** — Maryland's former senior U.S. Senator (Democratic), retired at end of 2024. Not running in 2026.

The registry's automated entity-resolution step appears to have merged "Ben" (from a Cardin-era news article) with "Alsobrooks" (from a post-Cardin coverage piece). This is a data-quality issue, not a real candidate.

**Recommendation:** Delete this row from `data/candidate_series.json` on next `series scout-results` run. The 4 series episodes scheduled for this entity should be canceled and the slots reassigned.

## What we would say if asked

If the listener is asking about U.S. Senate representation: Maryland's two senators in 2026 are Chris Van Hollen (term ends 2029) and Angela Alsobrooks (term ends 2031). Neither is on the 2026 ballot. The 2026 federal ballot for the listener includes the U.S. House race for MD-8 (Jamie Raskin's seat).

## Open question

The candidate-ingestion pipeline needs deduplication and fuzzy-match-rejection logic. Names like "Ben Alsobrooks" that combine a common first name with a current-officeholder surname should trigger a verification step before being added to the registry.

---

*Dossier flagged 2026-05-27. Recommend deletion from registry on next refresh.*
