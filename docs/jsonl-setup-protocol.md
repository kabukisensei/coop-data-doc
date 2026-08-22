# JSONL setup protocol

`coop-data-doc setup --transport jsonl` runs the same questionnaire as terminal setup.
Stdout is UTF-8 JSON Lines framed only by ASCII LF. A single trailing CR is accepted
on input. U+2028/U+2029 inside JSON strings are data, not frame boundaries. Lines over
1 MiB are rejected.

The producer emits `prompt`, `notice`, `progress`, and exactly one terminal event:

- success: `{"type":"complete","message":"Setup complete.","data":{"config":"coop-data-doc.yml"}}`, then exit 0
- cancellation: `{"type":"cancelled"}`, then exit 130
- protocol/runtime failure: `{"type":"error","message":"..."}`, then a non-zero exit other than 130

Prompt objects contain `id`, `kind`, `message`, `default`, and `choices`. The caller
answers with `{"id":"<same id>","answer":...}`. To cancel portably, send
`{"id":"<same id>","cancelled":true}`; the producer emits `cancelled` and exits 130.
IDs must match. Exit status is
authoritative, but consumers must also reject missing, duplicate, or contradictory
terminal events. Stderr is diagnostic text only and must never be parsed as protocol.
