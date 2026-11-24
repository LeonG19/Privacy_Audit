# Prompt v2 rerun: Enrique's half (mpci_bench, airgapagent_r, pisas)

**This folder now holds the Claude Sonnet 5.5 run.** Engine/model: `ENGINE=anthropic MODEL=claude-sonnet-5-5`, default `--seed 20261119`,
`--verify`, `prompt_version = variation-v2`. Command: Leon's Step 2 command with `--output-dir data/processed/variations/prompt_v2/enrique`.
The v2 prompt adds "Do not introduce information or details that are unnecessary to the context of the rewritten scenario" (end of rule 3 and a new rule 8).
Same 27 samples, same 95 variants, and the same replacement values as the v1 initial set.

The earlier Opus 5.5 run of the same prompt was replaced by this one. It is still in git history, together with its 95 graded rows,
at commit `445cdf1` on this branch.

## Results
- **95 ok, 0 failed.** 81 LLM rewrites + 14 MPCI seed substitutions (no LLM).
- **Verifier: 59 of 81 passed** (MPCI-Bench 15/27, AirGapAgent-R 8/9, PiSAs 36/45).
- The verifier is the same model as the generator, so these numbers are not directly comparable with Opus's 67/81.
  Sonnet's verifier is stricter on the MPCI stories, where it fails 9 rows that Opus passed.
  It is more lenient on PiSAs: it passes the 3 transmission-principle no-ops ("visible only to the sender" = the old value),
  which are no-ops by construction.

## Sonnet 5.5 vs Opus 5.5 (same prompt v2)
- **Fewer invented people.** Sonnet doesn't create "Dana" for the mapping-vehicle sender or "Riley" for the fandom sender,
  and it adds "Alex" to the PiSAs cast without inventing a title for him. It describes the new sender by role instead
  (e.g. "a mapping vehicle operator, who has come to hold copies of John's Facebook posts…").
- **The same distortions caused by the pool values remain:**
  - the political campaign still becomes a court case
  - the medication information type still leaves the bus-commute story in place
  - agent-style values ("The user, acting through the assistant") still end up in PiSAs casts
- Edits are slightly smaller: the median new/original length is 1.011, against 1.016 for Opus.
  11 of the 81 rewrites have exactly the same text as Opus's.

## The 22 verifier fails
- **MPCI-Bench (12).** Mostly "old value not fully removed":
  - trajectories still grant Calendar/Drive access
  - the Facebook family-conflict discussion is kept under a new information type
  - John is still the owner of the data when the subject changes
  - plus the known `39734048 sender` "recipe" mismatch
- **PiSAs subject changes (7).** The new subject is only added to one line, and the original person is still named in the cast and the other artifacts.
- **PiSAs (1 each):** a parental-consent medical rule added as a stray bullet (`thesis_readiness/27`), and a sender change that leaves Elena Okafor as the visibility holder (`special_event_permit_readiness/11`).
- **AirGapAgent-R (1):** "Sarah's clinical status" brings a named person into an attribute line.

## Review of the 95 rows (`review_enrique.csv`)
25 rows have exactly the same text as the Opus v2 run (the 14 seed substitutions, plus 11 rewrites that came out identical), so they keep those grades.
The other 70 were graded fresh. Each row has a note.

| column | y | ? | n | Opus v2 y |
|---|---|---|---|---|
| new value expressed | 85 | 10 | 0 | 87 |
| old value gone | 64 | 16 | 15 | **76** |
| other parameters preserved | 86 | 6 | 3 | 84 |
| natural + same format | 43 | 32 | 20 | 43 |
| replacement value sensible | 16 | 23 | 56 | 16 |

**Sonnet's main weakness is leaving the old value in.** It makes smaller edits, and often it only adds the new value as a prefix or one sentence:
- "For the Discharge Planning Coordinator: …"
- "Regarding the Special Event Permit Coordinator's review file: …"
- "the client" in one bullet while the original person is named everywhere else

The old permission sentence was also kept in two transmission-principle changes.

Its strength is that it invents fewer new people and keeps the other parameters slightly better.

**All 9 PiSAs information-type variants now replace the task-relevant fact.** In `special_event_permit_readiness/11`, Sonnet also dropped the compliant-certificate line, which Opus kept.
The correct outcome of these documents can change, so they shouldn't be used as-is for task-outcome comparisons.

The underlying problem is still the replacement values (56 of 95 are not sensible, the same count as with Opus), not the model or the prompt.
