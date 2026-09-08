# Lease-scoped box mailbox

The owner-side CLI, embedded `Harness.mailbox()` and `Mailbox` service now support
durable, versioned data messages. They do not create tasks, type into a terminal,
approve a plan, grant tools or satisfy an evaluator. The existing unversioned
`MESSAGE_ROUTED` task-message protocol remains compatible and is not silently
upgraded to claim acknowledgment or expiry guarantees it never had.

## CLI workflow

Use the actual state directory, run ID, frozen plan digest and exact box ID from
your current run. The three mailbox commands require an authenticated live
supervisor; `box list`, `resolve` and `read` still support offline inspection.

```sh
camol box observe "$BOX" --state-dir "$STATE" --run-id "$RUN" --plan-digest "$PLAN" > box-target.json
camol box message "$BOX" --state-dir "$STATE" --run-id "$RUN" --plan-digest "$PLAN" \
  --target-receipt box-target.json --request-id review-note-001 \
  --body 'Please inspect the documented edge case.' --kind question --ttl 300
camol box inbox "$BOX" --state-dir "$STATE" --run-id "$RUN" --plan-digest "$PLAN"
```

Repeat an uncertain send with the **same request ID and identical arguments**.
It returns the original record without another message. A changed request with
the same ID is denied. The receipt file must be bounded regular JSON; pipes and
devices are refused. Do not put credentials in message bodies or shell arguments.
`--sender` is attribution, not authentication or permission to act as that person.
The private supervisor token authenticates owner-side control access.

An observation binds run, plan, exact box, task, lease, fence, cursor and a lifetime
of at most 60 seconds. It cannot outlive its lease. Heartbeats and unrelated ledger
activity do not invalidate an unchanged generation; reassignment, expiry or a
different plan does. The receipt is a current-generation precondition, not a
cryptographic attestation that a human read text or that a provider is connected.
Observation/post reject boxes without running fenced tasks. A stopped, draining,
unreconciled or disconnected controller cannot queue delivery for a later reconnect.

## Delivery and acknowledgment

`BOX_MESSAGE_POSTED` records the sender (human or exact worker lease), recipient,
request ID, correlation ID, reply subject, kind, redacted body and expiry. Accepted
kinds are information, question, proposal and warning. Worker-supplied sender
claims cannot turn into human approval: the runner binds worker sends itself.

Pending messages enter the recipient's **next** context packet, with an explicit
data-not-authority policy. They never interrupt or inject keystrokes into a running
provider process. A completed task can leave a message undelivered; Camol does not
start new work merely to consume it. `BOX_MESSAGE_DELIVERED` means the packet was
prepared for that exact lease/turn, not that the provider ran or understood it.
Budget waits may delay launch after preparation.

Workers optionally return `message_acknowledgments: ["message-ID", ...]`.
Only IDs included in that actual packet may be consumed, and only after its worker
turn is recorded. Consumption is an explicit worker report, not verified
comprehension, task success or an evaluator result. Delivery/consumption retries
are idempotent. Expiry prevents new delivery; a receipt for an already-delivered
turn may arrive later and records `after_expiry: true`. A new lease never silently
inherits the old lease's messages.

At most 10 messages are included per packet, within an additional 8,000-character
ceiling and the remaining estimated frozen context envelope. Oversized/backlogged
items remain visible, not truncated or used to raise a budget. This character
estimate is not a provider token measurement. Each message is at most 2,000
characters, TTL is 1..3,600 seconds, each box has at most 100 pending messages, and
each run retains at most 10,000 messages. Inbox reads paginate up to 100 records.

Worker-authored versioned sends use `messages` entries with exactly `schema:
"camol.box_message_request"`, `target`, `request_id`, `body`, `kind`,
`correlation_id` and `ttl_seconds`. The caller must have a fresh target observation.
Rejected versioned sends produce `BOX_MESSAGE_SEND_REJECTED` with sender/turn,
request digest and reason; they do not retry unrelated successful build work.
Rejection is visible in the sender's box history and is not delivery. Applications
that require a reply as a completion condition must encode that in their frozen
task/evaluator contract rather than equating send acceptance with success.

## Inspection and remaining integration

The run ledger/export preserves posts, delivery, consumption and rejected sends.
Offline `box read` includes bounded mailbox records evaluated at the retained event
cut, labeled as **not live delivery proof**, plus sender-side rejection history.
The embedding API supports worker sends with an explicit current assignment;
worker-state storage remains owner-controlled, not exposed as a credentialed shell
bridge inside arbitrary workspaces.

Still open: TUI send/reply/inbox affordances, automatic fresh peer-observation
access for model tools, authenticated remote mailbox transport, and delegated
new-task/plan-change workflows. No auto-discovery tool server, remote delivery,
tiled pane layout or agent approval authority is claimed by this checkpoint.
