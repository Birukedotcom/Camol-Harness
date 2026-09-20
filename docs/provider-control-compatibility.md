# Installed provider control checks

On 2026-09-07, an explicitly scoped **non-inference** local check observed Claude
Code 2.1.263 and authenticated CLI status. Only version/authentication/help
commands ran; no prompt, quota probe, worker request or model call was sent.
No account identifiers or credentials are retained in this record.

The installed help reports the all-tools-off, strict empty MCP, empty settings,
safe-mode, output-format and budget controls, but omits `--max-turns`. Rejecting
every omitted help flag would therefore prevent the real installed planner and
preflight from working. The official [CLI reference](https://code.claude.com/docs/en/cli-usage)
documents `--max-turns` and explicitly notes that help is not exhaustive.

Camol does not drop the turn limit or trust an arbitrary version string. If the
only missing required flag is `--max-turns`, it performs one additional local
parser check using `--safe-mode --max-turns --help`, with no print mode or prompt.
The observed parser rejects `--help` as an invalid numeric value for that exact
known option. Camol accepts only the bounded recognized-option error patterns;
unknown options, successful help without recognition, other missing controls,
timeouts or unexpected output fail closed. The actual request still includes
`--max-turns 1`.

This proves parser/flag compatibility, **not** a successful model request, a
particular model entitlement, actual token usage, security of the installed
runtime, or enforcement of every limit. Live validation remains separate.

The [Agent SDK loop contract](https://code.claude.com/docs/en/agent-sdk/agent-loop)
also distinguishes successful result subtypes from turn/budget/execution errors.
Preflight must not infer success from answer text alone. It records known billing
on errors, and rejects conflicting or multi-model reports when producing its
single-model capability receipt. The [SDK option reference](https://code.claude.com/docs/en/agent-sdk/python)
describes the dollar stop condition as a client-side cost estimate; configured
dollar limits are not evidence of an account-wide billing or provider quota cap.
