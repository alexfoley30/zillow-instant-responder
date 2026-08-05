"""Every renter-facing string in one place. Voice: warm, casual, exact-time
booking, soft application mention, full signature. No em dashes in body copy
(the signature's em dash is Alex's sanctioned brand exception)."""

APP_LINK = "https://www.arizonaeliteproperties.com/vacancies"

SIGNATURE = (
    "Alex Foley\n"
    "Realtor & Property Manager\n"
    "Boundless Real Estate Arizona — Team Leader\n"
    "Powered by Arizona Elite Properties  |  License SA662452000\n"
    "480-815-9313  |  alex@azfoleyhomes.com  |  @alex.e.foley\n"
    "2425 S Stearman Dr, Suite 120, Chandler, AZ 85286"
)

WINDOWS_BLOCK = (
    "   Mon, Wed, Fri: 10:00 AM to 6:30 PM\n"
    "   Tue, Thu: 10:00 AM to 3:00 PM\n"
    "   Sat, Sun: 10:00 AM to 2:00 PM\n"
)

QUESTION_ACK = (
    "Great question! I'm checking on that for you and will follow up with an "
    "answer shortly. In the meantime, let's get you set up to see the place.\n\n"
)


def _lead_block(ack, answer):
    """One optional lead-in: a direct standing-rule answer beats the generic
    ack; never both, never a separate second email."""
    if answer:
        return answer + "\n\n"
    return QUESTION_ACK if ack else ""


def availability_ask(first_name, address, ack=False, answer=None):
    """Template 1 - first reply when no showing exists at the house."""
    return (
        f"Hi {first_name},\n\n"
        + _lead_block(ack, answer)
        + f"Thanks for reaching out about {address}! I'd love to get you in to see it.\n\n"
        "When are you hoping to move, and what day and time works to come take a look? "
        "Here's when I have open this week (Phoenix time):\n\n"
        f"{WINDOWS_BLOCK}\n"
        "Pick a time that works and I'll lock it in for you. Same-day tours are "
        "usually doable too if you give me a couple hours' notice. You can also start an "
        f"application here whenever you're ready: {APP_LINK}\n\n"
        "Looking forward to meeting you!\n\n"
        f"{SIGNATURE}"
    )


def offer_existing(first_name, address, when_human, ack=False, answer=None):
    """Template OE - consolidate-first: the house already has a showing."""
    return (
        f"Hi {first_name},\n\n"
        + _lead_block(ack, answer)
        + f"Thanks for reaching out about {address}! Great timing, we actually have "
        f"a showing already lined up there on {when_human} (Arizona time). "
        "Any chance you could make that one? I can add you right in.\n\n"
        "If that time doesn't work, no problem at all. Just tell me what day and "
        "time works for you and I'll get you set up. Here's when I have open this "
        "week (Phoenix time):\n\n"
        f"{WINDOWS_BLOCK}\n"
        "You can also start an application here whenever you're ready: "
        f"{APP_LINK}\n\n"
        "Looking forward to meeting you!\n\n"
        f"{SIGNATURE}"
    )


def booking_confirmation(first_name, address, when_human, agent_name):
    """Template 3 - slot booked (new event or folded into an existing one)."""
    meet = "I'll meet you there" if agent_name == "Alex Foley" \
        else f"{agent_name} will meet you there"
    return (
        f"Hi {first_name},\n\n"
        f"You're all set! I've got you booked for {address} at:\n\n"
        f"   {when_human} (Arizona time)\n\n"
        f"{meet}, so come on by at that time. If something comes up or you need a "
        "different time, just let me know and I'll get you rescheduled.\n\n"
        "You can also start an application here whenever you're ready: "
        f"{APP_LINK}\n\n"
        "See you then!\n\n"
        f"{SIGNATURE}"
    )


def counter_proposal(first_name, slots_human):
    """Template 4 - their time doesn't work; offer the nearest valid slots."""
    lines = "\n".join(f"   {s} (Arizona time)" for s in slots_human)
    return (
        f"Hi {first_name},\n\n"
        "Thanks for sending that over! That time doesn't quite work on my end, "
        "but here are a couple that do:\n\n"
        f"{lines}\n\n"
        "Let me know which one works and I'll lock it in.\n\n"
        f"{SIGNATURE}"
    )


def windows_ask(first_name):
    """Vague time ('sometime this weekend') - ask for an exact time."""
    return (
        f"Hi {first_name},\n\n"
        "Happy to get that set up! What exact day and time works best for you? "
        "Here's what I have open (Phoenix time):\n\n"
        f"{WINDOWS_BLOCK}\n"
        "Give me a specific time and I'll lock it in.\n\n"
        f"{SIGNATURE}"
    )


def leased_reply(first_name, address):
    return (
        f"Hi {first_name},\n\n"
        f"Thanks for reaching out about {address}! I'm sorry to say that home has "
        "been rented and is no longer available.\n\n"
        "Best of luck with your search!\n\n"
        f"{SIGNATURE}"
    )


def reschedule_after_cancel(first_name):
    return (
        f"Hi {first_name},\n\n"
        "No problem at all, thanks for the heads up! When would be a better "
        "time for you? Here's what I have open (Phoenix time):\n\n"
        f"{WINDOWS_BLOCK}\n"
        "Send me a time and I'll get you rebooked.\n\n"
        f"{SIGNATURE}"
    )


def stale_nudge(first_name, address):
    return (
        f"Hi {first_name},\n\n"
        f"Just checking back in about {address}. Still happy to get you in for a "
        "tour, and same-day usually works too. What day and time is good for you?\n\n"
        f"{SIGNATURE}"
    )


def apply_first(first_name):
    """Negotiation reply - ONE ACK EVER, never 'pass it to the owner'."""
    return (
        f"Hi {first_name},\n\n"
        "Totally understand wanting the best number! The owner only considers "
        "offers from screened applicants, so the first step is getting an "
        f"application in: {APP_LINK}\n\n"
        "Once you're approved, your offer goes to the owner formally. In the "
        "meantime I'd love to get you in to see the place. What day and time works?\n\n"
        f"{SIGNATURE}"
    )


def as_is_reply(first_name):
    """Modification requests (turf, paint, appliance swaps): home offered as-is."""
    return (
        f"Hi {first_name},\n\n"
        "Good question! The home is offered just as you see it, so we aren't "
        "able to make changes or additions. If it works for you as-is, I'd love "
        "to get you in for a look. What day and time is good?\n\n"
        f"{SIGNATURE}"
    )


def floor_plan_reply(first_name):
    return (
        f"Hi {first_name},\n\n"
        "We don't have floor plans or room measurements available for this one, "
        "so the best way to get a feel for the space is to come see it in person. "
        "What day and time works for you?\n\n"
        f"{SIGNATURE}"
    )


def fair_housing_steer(first_name):
    """Neighbors/area/schools questions: steer to visiting + public resources."""
    return (
        f"Hi {first_name},\n\n"
        "That's the kind of thing that's best to get a feel for yourself. I'd "
        "recommend visiting the neighborhood at a few different times of day and "
        "checking public resources online. Happy to get you in to see the home "
        "itself, though. What day and time works?\n\n"
        f"{SIGNATURE}"
    )


def checking_with_owner(first_name):
    """Property question we can't answer from the fact sheet."""
    return (
        f"Hi {first_name},\n\n"
        "Great question! Let me check on that and get right back to you. In the "
        "meantime, want to lock in a time to see the place? Here's what's open "
        "(Phoenix time):\n\n"
        f"{WINDOWS_BLOCK}\n"
        f"{SIGNATURE}"
    )


def approved_answer(first_name, body):
    """Alex's own words, approved via Poke - wrapped in greeting + signature
    only. No bot framing, no bolted-on tour pitch."""
    return (
        f"Hi {first_name},\n\n"
        f"{body.strip()}\n\n"
        f"{SIGNATURE}"
    )


def fact_answer(first_name, answer_line):
    """Answer drawn from the property fact sheet (one to two short sentences,
    already composed by the caller from sheet facts)."""
    return (
        f"Hi {first_name},\n\n"
        f"{answer_line}\n\n"
        f"{SIGNATURE}"
    )
