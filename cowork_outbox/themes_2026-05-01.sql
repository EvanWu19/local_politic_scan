-- weekly_themes UPSERT for window 2026-04-25 .. 2026-05-01
-- Apply on next cmd_publish run if not yet present.
INSERT INTO weekly_themes (
  week_start, week_end, themes, open_questions, underserved_topics,
  summary, note_count, generated_at, avoid_list, listener_candidate_interest
) VALUES (
  '2026-04-25', '2026-05-01',
  '["Concrete candidate voting records and historical public record","Property taxes and fiscal discipline","Sanctuary policy and immigration enforcement","Wootton High School closure","Candidate-vs-candidate side-by-side comparison on the listener''s actual primary ballot"]',
  '["Who among the Democratic primary candidates actually opposes sanctuary policy on illegal immigration?","Is Andrew Friedson the candidate who stands for fiscal restriction and spending discipline?","Why does MCPS want to close and move Wootton High School, and what happens next?","What is Kramer''s full historical voting record, including old news beyond recent sessions?","What are the documented policy differences between each candidate on my primary ballot, side-by-side?"]',
  '["Andrew Friedson''s specific fiscal record and votes","Wootton High School relocation timeline, appeal, and county-executive levers","The new MCPS rating system","Long-arc historical record of candidates pre-Council","Concrete head-to-head policy grid for County Executive and House District 19"]',
  'This listener is a first-time Democratic primary voter in Rockville (MD-08, Legislative District 19, Council District 7). What they want above everything else is concrete, candidate-by-candidate research tied to documented votes and historical public record. Their policy priors: skeptical of high property taxes, opposed to reflexive education-spending increases, supportive of fiscal discipline and non-essential spending cuts, specifically interested in which Democratic candidates oppose sanctuary policy on illegal immigration, and pointed about the Wootton High School relocation. Kids attend St. John''s Episcopal in Rockville, so MCPS coverage should not assume the family is inside MCPS.',
  7,
  '2026-05-01 11:00:00',
  '["The framing \"a listener asked us last week which Democratic primary candidates actually oppose sanctuary policy\" as a setup line","Evan Glass DHS-screening bill as the canonical sanctuary-policy example","ICE agent mask-ban ordinance as the sanctuary-policy benchmark","\"Two stories, properly worked through\" as an episode-framing device","Governor Moore''s \"Leave No One Behind\" slogan vs. disability cuts","ACA marketplace quiet-downgrade story","Maryland''s 90-day part-time legislature civics explanation","Wave of 25 Maryland lawmakers retiring framing","Beltsville Agricultural Research Center closure as District 19 hook","Listener''s frustration about deep-dives narrated back at them","\"Welcome to Rockville Politics Today\" + immediate two-item agenda preview","Standing recitation \"Glass, Jawando, and Friedson — all three are currently Councilmembers\"","Generic \"County Executive proposes the budget; council votes on it\" civics interlude"]',
  '["Andrew Friedson","Kramer","Crutchfield","Stewart","Tichy","Will Jawando","Evan Glass"]'
)
ON CONFLICT(week_start, week_end) DO UPDATE SET
  themes=excluded.themes,
  open_questions=excluded.open_questions,
  underserved_topics=excluded.underserved_topics,
  summary=excluded.summary,
  note_count=excluded.note_count,
  generated_at=excluded.generated_at,
  avoid_list=excluded.avoid_list,
  listener_candidate_interest=excluded.listener_candidate_interest;
