"""One live call to the backup AI with a sample renter reply and a sample
proofread, using the same prompts and schema checks as production. Sends no
email and touches no ledger. Needs FALLBACK_LLM_BASE_URL, FALLBACK_LLM_API_KEY
and FALLBACK_LLM_MODEL in the environment.

    python3 scripts/fallback_check.py
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm  # noqa: E402


def main() -> int:
    if llm.fallback_provider_tag() is None:
        print("Backup AI is not configured (need FALLBACK_LLM_BASE_URL, "
              "FALLBACK_LLM_API_KEY, FALLBACK_LLM_MODEL).")
        return 2
    now = datetime(2026, 10, 26, 9, 0)  # a Monday morning, Phoenix
    renter = "Could I come see it Wednesday at 4pm? I get off work at 3."
    transcript = ("US: When works for you to come take a look?\n"
                  f"RENTER: {renter}")
    cls = llm._fallback_json(llm._classify_prompt(renter, transcript, now),
                             llm.SCHEMA, "renter_reply", "classify")
    print("classify:", json.dumps(cls, indent=2) if cls else "FAILED")
    review = llm._fallback_json(
        llm.REVIEW_PROMPT.format(
            now=now.strftime("%A, %B %d, %Y at %I:%M %p"),
            transcript=transcript, template="booking_new",
            outgoing="You're all set for Wednesday, October 28 at 4:00 PM."),
        llm.REVIEW_SCHEMA, "send_review", "review")
    print("review:", json.dumps(review) if review else "FAILED")
    ok = bool(cls) and bool(review)
    print("OK, the backup works" if ok else "NOT OK, see the errors above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
