# JSONL setup protocol (version 1.1, shipped in coop-data-doc 1.1.1)

`coop-data-doc setup --transport jsonl` runs the same questionnaire as terminal setup.
Stdout is UTF-8 JSON Lines framed only by ASCII LF. A single trailing CR is accepted
on input. U+2028/U+2029 inside JSON strings are data, not frame boundaries. The line
limit covers content after the trailing LF: a line whose content is exactly 1 MiB of
characters is accepted; anything over is rejected as a protocol error. A final input
line without a trailing LF is tolerated.

The FIRST event on stdout is always the hello handshake:

- `{"type":"hello","protocol_version":"1.1"}`

Consumers may ignore it, but must not treat it as a prompt or terminal event. After
hello, the producer emits `prompt`, `notice`, and exactly one terminal event.
(`progress` is a RESERVED event type: declared for forward compatibility and handled
by consumers, but nothing emits it today.) Terminal events:

- success: `{"type":"complete","message":"Setup complete.","data":{"config":"coop-data-doc.yml"}}`, then exit 0
- cancellation: `{"type":"cancelled"}`, then exit 130
- protocol/runtime failure: `{"type":"error","message":"..."}`, then exit 2

Prompt objects contain `id`, `kind`, `message`, `default`, and `choices`. The caller
answers with `{"id":"<same id>","answer":...}` where `answer` is typed by kind:

| kind     | answer type | notes |
|----------|-------------|-------|
| `text`   | string      | empty/missing answer falls back to the prompt default |
| `path`   | string      | same default fallback as text |
| `confirm`| boolean     | any non-boolean is a protocol error (exit 2) |
| `select` | string      | exactly one offered `Choice.value`; labels, case changes, and whitespace changes are invalid |
| `checkbox`| list       | every item is a string exactly matching an offered value; duplicates are invalid; an empty list is valid |

To cancel portably, send `{"id":"<same id>","cancelled":true}`; the producer emits
`cancelled` and exits 130. IDs must match — `id` is validated BEFORE `cancelled`,
so a cancel with the wrong id is a protocol error (exit 2), not a cancellation.
Exit status is authoritative, but consumers must also reject missing, duplicate, or
contradictory terminal events. Stderr is diagnostic text only and must never be
parsed as protocol.
