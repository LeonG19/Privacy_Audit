# Variations: Enrique's half (mpci_bench, airgapagent_r, pisas)

Engine/model: `ENGINE=anthropic MODEL=claude-opus-5-5`, default `--seed 20261119`. Samantha should use the same model.
The commands are exactly the ones in `docs/variation_handoff.md`.

## Code fix needed before anything ran
`anthropic` SDK 2.x removed `temperature` from `messages.create()`, so every call crashed with a `TypeError` before being sent.
There is no flag to avoid it (`--temperature` is always sent). `src/ci_audit/engines/anthropic_api.py` now drops the parameter,
the same way it already handled the API's 400, and records it in `generation.dropped_params`. Two tests were added and 195/195 pass.
No prompt or pool was changed. Samantha needs this fix too if she uses the `anthropic` engine.

## Step 1 (smoke): ready
`smoke/enrique/`, exact command (`--per-dataset 1 --verify`): 3 samples (MPCI story, AirGapAgent-R, PiSAs museum loan), 9 variants.
**9 ok, 0 failed**, nothing empty, cut off or identical. Verifier 8/9.
Read by hand: the new value is expressed, the old value is gone, the other parameters are kept, and the format is kept.
- The one fail: the pool value "Alyssa, as **Kyle's** conversation partner" comes from another scenario. The model adapted it to this scenario's names
  (system rule 3), and the verifier flagged that.
- The random draw contained no trajectory, so I also ran one sample per format (`smoke/enrique_formats/`: MPCI story ×2, MPCI trajectory,
  AirGapAgent-R, PiSAs; 19 variants). 19 ok, 0 failed, verifier 19/19. The trajectory kept its Action/Observation layout and the PiSAs document its cast/artifact layout.

## Step 2 (initial set): partial, the API key ran out of credits (sent as is)
`initial_review/enrique/`, exact command (`--per-domain 3 --domains-per-dataset 3 --verify`): 27 samples, 95 variants planned.
- Domains: MPCI-Bench Government, Personal relationships and Retail; AirGapAgent-R Healthcare, Retail and Workplace; PiSAs Education, Government and Healthcare.
- **53 ok, 42 failed.** Every failure is `400 "Your credit balance is too low"`; none is a content failure.
- **MPCI-Bench (41) and AirGapAgent-R (9) are complete.** PiSAs has only 3 of its 45 variants (all from `thesis_readiness/scenario_15`),
  and 2 of those 3 have no verifier result. As agreed, we're sending what we have. The rest can be finished with the same command + `--resume`.

## Review of the 53 rows (`review_enrique.csv`)
Every row was graded from a side-by-side diff of the original and new text, and each has a note.

| column | y | ? | n |
|---|---|---|---|
| new value expressed | 50 | 2 | 1 |
| old value gone | 50 | 2 | 1 |
| other parameters preserved | 49 | 4 | 0 |
| natural + same format | 33 | 15 | 5 |
| replacement value sensible | 13 | 12 | **28** |

The rewriting itself is mostly correct. The weak point is the pools: over half the replacement values come from other scenarios or don't fit the role
(other scenarios' names like Jane, Emily, Alex and Julia; agent-trajectory values like "The user, acting through the assistant" in stories and seeds;
"Your local library" as the subject of a license plate). Most "natural ?/n" grades are the model contorting the story to fit those values.
- In the finished part: 39 LLM rewrites + 14 MPCI seed substitutions. Verifier passed 35/37 of the ones it could check.
  - `mpci_bench:2017_67125926_pos:trajectory::transmission_principle`: the new principle was added and the old one kept. **I agree, a generator miss.**
  - `mpci_bench:2017_39734048_pos:trajectory::sender` → "Jane Smith, as the prospective sharer **of the recipe**": there is no recipe in this scenario.
    The pool value doesn't fit, so the model only added "I'm Jane Smith".

The seeded draw picked `airgapagent_r:full:04:job_interview:pet_ownership` and `…:17:job_interview:pet_ownership`, which have
**identical texts**. They are effectively one duplicate sample.

## Things the rewrite / pools get systematically wrong
(Seen in this run and in the extra runs above.)
1. **PiSAs information type bundles the task-relevant fact with the private detail.** For example "Mira's reason for the higher limit **and** the
   compliant $5M coverage". Replacing the information type also removes the task fact, so the document's correct outcome
   (resolve_insurance / ready / …) can change.
2. **PiSAs transmission principles were extracted as visibility tags** ("Visible only to X"). Agent-style pool values ("the user explicitly
   authorizes the agent to consult Messenger…") end up stuffed into the `(visible to …)` header and read awkwardly.
   The plan's "never the original value" check also misses paraphrases: "Visible only to Felix Diop" → "visible only to the sender" is the same principle.
3. **Pools contain other scenarios' sentences, names and tool permissions.** Examples:
   - AirGapAgent-R "Profile attribute: employee_feedback_and_aggregate_findings (workload, stress, …)", "clinical_status (…)",
     "festival_images, correspondence_with_participants…"
   - subjects like "restaurant recommendation service" or "client"
   - a transmission principle naming "Calendar and GoogleDrive", which made the model delete a Slack search step from a trajectory

   I mark these `replacement_value_sensible = n`.
4. **To make an odd value fit, the model sometimes changes the stated purpose or context.** Example: `45496593 story::recipient` → "The Defendant and
   courtroom participants" turned the political campaign into a court case. This is close to the prompt's "no new purpose" rule.
5. AirGapAgent-R attribute spelling is inconsistent: usually snake_case like the original, sometimes "sexual orientation" with a space.
