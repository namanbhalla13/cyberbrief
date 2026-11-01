# Assistant fixtures

All conversations here are fictional.

## `conversations/` — scripted replies for fixture mode

Each file is one scripted conversation. `FixtureAssistantModel` matches the citizen's exact lines so
far (normalised) to a scripted reply in the same JSON shape the live model must return
(`backend/app/assets/assistant_reply_v1.json`). Anything that does not match gets a fixed reply that
says the demo can only follow the suggested replies.

| File | Starts | What it shows |
|---|---|---|
| `chat-register.json` | without a complaint | the register suggestion, a draft identical to the Demo A typed account (so its extraction fixture replays), then status, promise bait, off topic and submitting |
| `chat-lookups.json` | from a complaint | the five read-only lookups, each answered with a card fetched from the record: complaint details, files read, what was noted, the citizen's complaints, the next step |
| `chat-status.json` | from a complaint | status from the record, Hinglish promise bait, a request to call the police |
| `chat-off-topic.json` | without a complaint | off topic, then promise bait |
| `chat-hindi.json` | without a complaint | how CyberBrief works, in Hindi |

A turn marked `requiresCase` is offered only once the chat is linked to a complaint.
`publicStartPack` names the curated case the start quick reply loads in public mode.

`test_every_scripted_reply_validates_and_passes_its_own_checks` keeps every scripted reply inside
the same rules as a live one.

## `eval/` — labelled conversations for `scripts/run_assistant_eval.py`

`development/` and `holdout/` hold conversations with expectations per turn: the suggestion the rules
should make, whether the turn is off topic, help entries that must appear, whether a status card is
due, and the reply language. Do not tune the prompt on `holdout/`.

In fixture mode only `dev-00-scripted-register` can be answered; the rest are reported as
unscripted and not scored.
