# Prompt to build the initial set from extracted records (ChatGPT / Claude chat)

Use this so everyone's initial set comes out in the same format. Paste the prompt, then paste your extracted records
(one per line is enough: record id, domain, sender, subject, recipient, information type, transmission principle,
purpose). Do it once per domain if the data is long.

---

You are helping build a set of Contextual Integrity (CI) norm elements per social domain, following Shvartzshnaider et
al., "Learning Privacy Expectations by Crowdsourcing Contextual Informational Norms" (HCOMP 2016). Each CI norm is a
5-tuple (sender, subject, recipient, attribute, transmission principle), and the elements are grouped as:

- **Roles of actors** (senders, subjects, recipients)
- **Attributes** (information types)
- **Transmission principles** (constraints on the flow), written as conditions like the paper's:
  "Knowledge: If the ⟨sender⟩ let the ⟨subject⟩ know", "Permission: If the ⟨sender⟩ asked for the ⟨subject⟩'s
  permission", "Breach of contract: If the ⟨subject⟩ is performing below a certain standard".

Below are CI parameters already extracted from benchmark records, with their domain. For each domain:

1. List **Examples of Roles**, then say which act as sender, subject and recipient. Replace personal names with the
   person's role (job title or relationship). Merge duplicates.
2. List **Examples of Attributes**. Generalize specific facts into attribute types (e.g. "Morgan uses a wheelchair" →
   "Medical condition / accessibility needs"), but keep them concrete enough to put in a survey question. Merge
   duplicates.
3. List **Examples of Transmission Principles** in the ⟨sender⟩ / ⟨subject⟩ / ⟨recipient⟩ template form. Only use
   conditions that appear in the data; do not add new ones.

Rules: use only what is in the data; do not invent roles, attributes or principles. If a domain has no data for one of
the three groups, write "none in the data". Output one section per domain in Markdown.

DATA:
(paste records here)
