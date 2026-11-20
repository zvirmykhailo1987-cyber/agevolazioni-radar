# Five-minute walkthrough

For an interview or a screen share. Live demo address and sign-in are in the README.

1. **Panoramica (30 s).** The knowledge base is rebuilt every working day from INPS. Point at freshness ("last act
   read") and at the eval chart: answers state the expected facts in N% of held-out questions, and the system refuses
   when the sources are silent. The numbers come from saved eval runs, not from a slide.

2. **Catalogo → Bonus ZES 2026 (60 s).** Every value has its paragraph and the exact words it was read from. Show a
   "non indicato" field: nothing is guessed. Show "Differenze tra atti": circular 56 admits *somministrazione*, the
   later message does not mention it; the app does not pick one, a person does. Show the act timeline and, where an
   act changed, the text diff.

3. **Recupero → Trattoria del Porto (90 s).** Four hires scanned against every active incentive. Luca (28, 30 months
   without a regular job, Puglia) gets Bonus Giovani with the ZES cap; Maria (42, 26 months) gets Bonus ZES or Bonus
   Donne, one proposed, one alternative, because they are not cumulable. Giulia (4 months) and the apprentice get
   nothing, and the page says why. Open a finding: each criterion with its quote, the conditions the employer must
   attest (no dismissals in the previous six months, de minimis), and the data still to collect. Change the
   employer contribution rate: the estimate is an assumption, and the page says so.

4. **Sign in and decide (40 s).** Approve one proposal with a note. Open **Stato**: the decision is in the audit log
   with who and when; below, every LLM call with tokens and latency, and the pipeline loads.

5. **Chiedi (40 s).** Ask "Qual è il tetto mensile del Bonus Donne nella ZES?": answer with citations. Ask something
   the sources do not cover ("Bonus Giovani 2028?"): refusal, not a guess.

6. **Close (20 s).** Same pieces as the job description: dlt + dbt pipeline with versioned knowledge, agents that act
   only through deterministic tools, an MCP server any agent can use, and evals that gate releases.

Questions worth expecting, with the honest answer:
- *Why not let the model decide eligibility?* Law reserves the decision to a professional, and a decision has to be
  explainable criterion by criterion. The model reads; code decides; a consultant approves.
- *How do you know it works?* Held-out questions never used for tuning, three runs each, an agent answer key written
  from the circulars, a mutation check on the guarantees, and a regression gate on every metric.
- *What breaks first in production?* Retrieval misses (the system refuses instead of guessing) and incentives the
  schema cannot express (they become "condizioni da verificare", never a silent pass).
