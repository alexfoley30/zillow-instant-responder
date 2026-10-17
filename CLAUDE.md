# Working on this repo

## Who you are talking to

Alex Foley owns this business and this code. He is not a programmer and does
not want to become one. He asked, on 2026-09-07, that explanations always be
written the way you would explain it to someone who knows nothing about
computers. That request stands until Alex himself says otherwise.

## How to write to him

Explain in terms of the business, not the code:

- Renters, emails, tours, the calendar, the "list of houses that are already
  rented". Not: functions, locks, Firestore, transactions, mocks.
- Say what could go wrong for a real person. "A renter gets the same email
  twice" beats "idempotency violation in reserve_send".
- Use a plain comparison when it helps (the send lock is a deli-counter
  ticket; a test is a fire drill), then stay inside that comparison.
- Short paragraphs. Headings so he can skim. No walls of text.
- Say plainly what changed for renters, what changed for him, and what he has
  to do next, if anything. If the answer is "nothing yet, it's waiting for
  you", say exactly that.
- Never make him ask what a word meant. If a technical name is unavoidable
  (a file he'll see, a branch, a PR number), say it once and say what it is.

Being simple is not the same as being vague. Do not hide a risk, a failure, a
guess, or a thing you did not finish because it would take a paragraph to
explain. Explain it in the paragraph.

Code, commit messages, PR bodies and code comments stay technical and precise
as usual. This is about what you write TO Alex, in chat, in a notification, or
in a summary he will read.

## Ground rules for the work itself

- Alex reviews and merges. Never merge, never push to `main`.
- `DRY_RUN=true` is the safe default: it writes what it WOULD have sent to
  `zillow_shadow` instead of emailing anyone. Keep it that way in tests.
- Tests call the real production functions with realistic data. A test that
  re-implements the logic it is testing, or asserts only that a function was
  called, is how a broken fix once shipped as fixed (see the header of
  `tests/test_audit_regressions.py`).
- Run the suite before proposing anything: `python -m pytest -q`.
  `pytest` is not in `requirements.txt`; install it separately.
