# How auto-apply actually works

Written because "why didn't it fill that field?" should be answerable without reading code.
Every claim here is checked by `test_engine.py`.

---

## 1. Where your data lives

There is **one** row per user in the Supabase `profiles` table. Everything the agent knows
about you is in it.

| Column | Holds | Set from |
|---|---|---|
| `name`, `email`, `title`, `contact` | identity | Workbench → Profile |
| `summary`, `skills` | used for résumé tailoring **and** job ranking | Workbench → Profile |
| `data.exp`, `data.education`, `data.proj` | your history — the résumé is built from these | Workbench → Profile |
| `data.standing` | **the answer bank** — see below | Add answers, Workbench → System |
| `data.orchestration` | agent settings (tailoring engine, cover letters…) | Agent board |

### `data.standing` — the answer bank

This is what fills forms. Two parts:

```jsonc
"standing": {
  "phone": "+1 555 010 2020",          // structured facts, matched by meaning
  "work_authorized": "Yes",
  "needs_sponsorship": "Yes",
  "current_location": "San Jose, CA",

  "_persona": "International student on F-1/OPT…",   // frames ambiguous answers

  "_custom": {                          // verbatim question → answer
    "Location (City)": "Buffalo, New York",
    "What is your current notice period?": "2 weeks"
  }
}
```

- **Structured facts** are matched by meaning: a `phone` fact answers "Phone", "Mobile number"
  and "Contact number" alike (`ANSWER_KEYS` in `apply_browser.py`).
- **`_custom`** stores the exact question you answered. A reworded version of the same
  question on another board still matches, by salient-token fingerprint (`_fp`).

Everything you save through **Add answers** lands here, and is reused on every future
application. That is why answering once is worth it.

Applications are stored separately in `applications`, one row per (user, job), with the full
receipt — including the decision trace described below.

---

## 2. What happens when you click Auto

```
 route  →  navigate  →  extract  →  plan  →  fill  →  honest gate
```

**route** — `board_agents.route(url)` picks the agent that knows this board: greenhouse,
lever, ashby, smartrecruiters, recruitee, icims, or generic. Workday is deliberately
unsupported: it requires creating an account with a password, which the agent will not do.

**navigate** — gets from the link to the real form. Each board hides it differently: Lever
puts it at `/<id>/apply`, SmartRecruiters behind an "I'm interested" button, iCIMS inside an
iframe. Up to three hops, and it follows a new tab if one opens.

**extract** — reads **every** field into a schema before filling anything:

```
label · type · required · sensitive · options
```

Types are real: `text`, `textarea`, `select`, `radio`, `checkgroup`, `combo` (a typeahead),
`date`, `consent`, `yesno`. A React combobox is an input *with* a listbox, so it is collapsed
to one control rather than counted twice.

**plan** — decides every field at once, in this order of authority:

1. **Your saved answers win.** Matched by meaning, then by fingerprint.
2. **Sensitive questions are yours alone.** Legal status, sponsorship, compensation,
   demographics, criminal history. The model never answers these, and never *reinterprets*
   them — asked to map a "Yes" onto a Right-to-Work select, it once chose "I have the right
   to work without sponsorship" for a candidate who needs sponsorship. That is a false
   statement on a real application, so the path is closed.
3. **Legal agreements** (arbitration, terms) are left for you to accept.
4. **Marketing opt-ins** are declined by default.
5. **Everything else still unanswered** goes to the model — in *one* call that sees the whole
   form, with your résumé, your stated facts and your persona as context.

**fill** — applies the plan. Options are matched by score, not first-hit: the leading token
carries most of the weight, extra words are penalised, US state names fold to their codes. If
nothing scores well enough the field is left **blank** and reported. A wrong city on a real
application is worse than an empty one.

**honest gate** — the agent never clicks Submit while a required question is unanswered, and
never without a résumé attached. It reports what is missing instead of sending something
half-filled and calling it sent.

---

## 3. Why a field was or wasn't filled

Open **Details** on any application. The table shows one row per field:

| Question | Type | Answer | From | Why |
|---|---|---|---|---|
| Location (City) | combo | Buffalo, New York | profile | you answered this before |
| Right to Work status | select | — | unanswered | your saved answer "Yes" is not one of this form's options, and it is a legal question |
| Current total compensation | text | — | unanswered | compensation — never answered by the model |
| Happy to work 4 days in office? | select | Yes | profile | you answered this before |

`From` is one of:

- **profile** — your own saved answer
- **llm** — the model, from your résumé and stated facts
- **default** — a marketing opt-in, declined
- **unanswered** — with the reason given

If the model wasn't called at all, the panel says why. Usually: *"every required question was
already answered from your profile, or is one only you may answer."* That is the system
working, not the model being ignored.

The same panel shows the **exact system prompt**, the context the model was given, and its
**raw response** — so a wrong answer can be traced to its cause rather than guessed at.

---

## 4. Making it fill more

1. **Fill in your profile properly** — title, skills, experience. Ranking and résumé quality
   both come from here, and an empty profile scores every job at the floor value of 5.
2. **Answer once, via Add answers.** Each answer is reused on every future application,
   including reworded versions of the same question.
3. **Match the form's wording for option fields.** A saved "Yes" cannot fill a select whose
   choices are full sentences. The Add-answers dialog shows the real options for exactly this
   reason.
4. **Use a hosted model** if free-text answers matter to you. The local model (Ollama) refuses
   more questions than a hosted one; set `CLAUDE_API_KEY` or `OPENAI_API_KEY` in `.env`.

---

## 5. What it will not do

- Solve or bypass a CAPTCHA. Blocked boards are reported so you can finish them yourself.
- Create an account or enter a password (this is why Workday is unsupported).
- Answer a legal, compensation or demographic question on your behalf.
- Submit a form with required questions unanswered, or without a résumé.
- Report an application as sent when it wasn't.
