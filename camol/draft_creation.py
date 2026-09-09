"""Human-reviewed creation envelopes for unapproved V5 model proposals.

The envelope constrains drafts; it is not execution approval or evidence that a
proposed oracle establishes a prose requirement. No subprocess or model runs here.
"""

import copy
import json
import re
import shlex
from dataclasses import replace
from pathlib import Path

from .planning import PlanningError, _items, _required_text, reject_sensitive_text
from .proposal import MAX_PROMPT_CHARS, MAX_RESPONSE_CHARS, strict_json
from .providers import load_model_profile, ModelProfile
from .runbook import validate_runbook
from .schema import canonical_digest, require_identifier, require_digest


QUESTIONS = (
    ("outcomes", "What observable outcomes make this finished? Use separate lines or semicolons."),
    ("exclusions", "What must not change or happen? Deployment, uploads, downloads and credential changes are not authorized by this wizard."),
    ("invariants", "What must remain true through retries, failures and integration? State the behavior, not just 'tests pass'."),
    ("oracles", "How could each outcome or invariant be disproved? Name existing checks, expected examples, or say unknown; unknown coverage must become questions."),
    ("worker", "Choose the worker runtime explicitly: `claude PROFILE`, `codex PROFILE`, `codex-oss PROFILE`, or `process --sandboxed COMMAND {packet} {result}`. Use `process --trusted ...` only to propose the explicitly weaker host tier. Example Claude profile: @camol/claude-fable-5-1. No runtime is executed now."),
    ("limits", "Review limits as `boxes=N concurrency=N turns=N tokens=N cost_cents=N timeout=N tasks=N attempts=N`. Type defaults to review the displayed conservative defaults. Process cost is 0; provider cost is a frozen ceiling/reservation, not automatically a hard billing cap."),
)
DEFAULT_LIMITS = dict(boxes=1, concurrency=1, turns=6, tokens=48000, cost_cents=100,
                      timeout=1800, tasks=8, attempts=3)
BOUNDS = dict(boxes=(1, 32), concurrency=(1, 32), turns=(1, 100), tokens=(1000, 1000000),
              cost_cents=(0, 10000), timeout=(30, 7200), tasks=(1, 32), attempts=(1, 10))
COMPLETION = ["all_tasks_succeeded", "all_required_evidence_present", "all_verifications_green", "no_open_blockers", "no_open_debug_cases"]
AUTHORITY_RULE = dict(id="human-authority", text="No work before exact approval; no deployment, uploads, downloads or credential changes are authorized.", enforcement="hard")


def creation_state(goal, source, owner, model, effort, run_id):
    return dict(schema="camol.creation_draft", schema_version=1, goal=_required_text(goal, "goal", 1500),
                source=copy.deepcopy(source), owner=owner, planning_model=model, effort=effort, run_id=run_id,
                answers={}, question_index=0, phase="questioning", envelope=None, reviewed_digest=None,
                questions=[], clarifications=[], risk_reviewed_digest=None)


def is_creation(value):
    return isinstance(value, dict) and value.get("schema") == "camol.creation_draft"


def validate_state(value):
    fields = {"schema", "schema_version", "goal", "source", "owner", "planning_model", "effort", "run_id",
              "answers", "question_index", "phase", "envelope", "reviewed_digest", "questions", "clarifications", "risk_reviewed_digest"}
    if not is_creation(value) or set(value) != fields or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PlanningError("creation draft state has missing or unknown fields")
    if value["phase"] not in {"questioning", "review", "confirmed", "clarifying", "candidate"}:
        raise PlanningError("creation draft phase is invalid")
    for field in ("goal", "owner", "planning_model", "effort", "run_id"):
        _required_text(value[field], "creation " + field, 1500)
    if not isinstance(value["answers"], dict):
        raise PlanningError("creation answers must be an object")
    index = value["question_index"]
    if type(index) is not int or not 0 <= index <= len(QUESTIONS) or set(value["answers"]) != {name for name, _ in QUESTIONS[:index]}:
        raise PlanningError("creation questions and answers do not match")
    if (value["phase"] == "questioning") != (index < len(QUESTIONS)):
        raise PlanningError("creation phase does not match completed questions")
    for answer in value["answers"].values():
        _required_text(answer, "creation answer", 4000)
    if not isinstance(value["questions"], list) or len(value["questions"]) > 8:
        raise PlanningError("creation follow-up questions are invalid")
    for item in value["questions"]:
        _required_text(item, "creation question", 1500)
    if bool(value["questions"]) != (value["phase"] == "clarifying"):
        raise PlanningError("creation follow-up questions do not match its phase")
    _clarifications(value["clarifications"])
    if value["risk_reviewed_digest"] is not None:
        require_digest(value["risk_reviewed_digest"], "risk review digest")
    if (value["envelope"] is None) != (value["phase"] == "questioning"):
        raise PlanningError("creation envelope does not match its phase")
    if value["envelope"] is not None:
        validate_creation_envelope(value["envelope"])
        if any(value["envelope"][field] != value[field] for field in ("goal", "source", "owner", "planning_model", "effort", "run_id", "clarifications")):
            raise PlanningError("creation envelope conflicts with its human draft identity")
        if value["reviewed_digest"] not in {None, canonical_digest(value["envelope"])}:
            raise PlanningError("creation envelope confirmation is stale")
        if value["phase"] in {"confirmed", "candidate"} and value["reviewed_digest"] is None:
            raise PlanningError("creation phase requires an exact envelope confirmation")
    return copy.deepcopy(value)


def _clarifications(items):
    if not isinstance(items, list) or len(items) > 32:
        raise PlanningError("creation clarifications exceed the bounded dialogue; restart a narrower draft")
    for item in items:
        if not isinstance(item, dict) or set(item) != {"question", "answer"}:
            raise PlanningError("creation clarification has missing or unknown fields")
        _required_text(item["question"], "clarification question", 1500)
        _required_text(item["answer"], "clarification answer", 4000)


def question(state):
    state = validate_state(state)
    if state["phase"] == "questioning":
        text = QUESTIONS[state["question_index"]][1]
        if state["question_index"] == len(QUESTIONS) - 1:
            defaults = dict(DEFAULT_LIMITS)
            if state["answers"]["worker"].startswith("process "):
                defaults["cost_cents"] = 0
            text += " Defaults: " + " ".join("{}={}".format(key, value) for key, value in defaults.items())
        return text
    if state["phase"] == "clarifying" and state["questions"]:
        return state["questions"][0]
    return None


def parse_limits(text, process=False):
    result = dict(DEFAULT_LIMITS)
    if process:
        result["cost_cents"] = 0
    if text.strip().lower() == "defaults":
        return result
    seen = set()
    for item in text.split():
        match = re.fullmatch(r"([a-z_]+)=(\d+)", item)
        if not match or match[1] not in BOUNDS or match[1] in seen:
            raise PlanningError("limits require each supported name=N once, or defaults")
        key, number = match[1], int(match[2])
        if not BOUNDS[key][0] <= number <= BOUNDS[key][1]:
            raise PlanningError("limit {} is outside its supported bound".format(key))
        result[key] = number
        seen.add(key)
    if not seen or result["concurrency"] > result["boxes"]:
        raise PlanningError("limits must be non-empty and concurrency cannot exceed boxes")
    if process and result["cost_cents"] != 0:
        raise PlanningError("process workers have no provider-cost measurement; select cost_cents=0")
    return result


def _worker(text, workspace, limits, effort):
    parts = shlex.split(text)
    if not parts:
        raise PlanningError("choose an explicit worker runtime")
    if parts[0] == "process":
        if len(parts) < 3 or parts[1] not in {"--sandboxed", "--trusted"}:
            raise PlanningError("process selection requires --sandboxed or explicitly weaker --trusted, then exact argv")
        argv = parts[2:]
        if not all(any(marker in item for item in argv) for marker in ("{packet}", "{result}")):
            raise PlanningError("process worker argv must include {packet} and {result}")
        return dict(kind="process", argv=argv, timeout_seconds=limits["timeout"]), (
            "developer_sandboxed" if parts[1] == "--sandboxed" else "developer_trusted")
    choices = {"claude": "claude_cli", "codex": "codex_cli", "codex-oss": "codex_oss"}
    if len(parts) != 2 or parts[0] not in choices:
        raise PlanningError("worker must name an existing supported provider plus exact profile, or a process argv")
    profile = load_model_profile(Path(workspace), parts[1])
    if profile.adapter_kind != choices[parts[0]]:
        raise PlanningError("selected profile belongs to a different worker runtime")
    if not 1 <= limits["cost_cents"] <= profile.max_run_usd_cents:
        raise PlanningError("provider cost_cents must be positive and cannot exceed the selected profile")
    profile = replace(profile, effort=effort, max_agent_turns=min(limits["turns"], profile.max_agent_turns),
                      max_turn_tokens=min(8000, limits["tokens"], profile.max_turn_tokens),
                      max_run_usd_cents=limits["cost_cents"],
                      max_task_usd_cents=min(profile.max_task_usd_cents, limits["cost_cents"]),
                      max_turn_usd_cents=min(profile.max_turn_usd_cents, limits["cost_cents"]))
    return dict(kind=profile.adapter_kind, profile=parts[1], profile_snapshot=profile.to_dict(),
                timeout_seconds=limits["timeout"]), "developer_trusted"


def answer(state, text, workspace):
    state = validate_state(state)
    text = _required_text(text, "draft answer", 4000)
    if state["phase"] == "clarifying":
        state["clarifications"].append(dict(question=state["questions"].pop(0), answer=text))
        state["envelope"]["clarifications"] = copy.deepcopy(state["clarifications"])
        state["reviewed_digest"] = None
        if not state["questions"]:
            state["phase"] = "review"
        return state
    if state["phase"] != "questioning":
        raise PlanningError("the creation envelope is ready; inspect /draft and confirm its exact digest")
    name = QUESTIONS[state["question_index"]][0]
    if name == "worker":
        preliminary = dict(DEFAULT_LIMITS, cost_cents=0) if text.startswith("process ") else dict(DEFAULT_LIMITS, cost_cents=1)
        _worker(text, workspace, preliminary, state["effort"])
    if name == "limits":
        parse_limits(text, process=state["answers"]["worker"].startswith("process "))
    state["answers"][name] = text
    state["question_index"] += 1
    if state["question_index"] == len(QUESTIONS):
        state["envelope"] = build_envelope(state, workspace)
        state["phase"] = "review"
    return state


def build_envelope(state, workspace):
    answers = state["answers"]
    limits = parse_limits(answers["limits"], process=answers["worker"].startswith("process "))
    adapter, tier = _worker(answers["worker"], workspace, limits, state["effort"])
    requirements = [dict(requirement_id="{}-{}".format(kind, index), kind=kind, text=text)
                    for kind in ("outcomes", "invariants") for index, text in enumerate(_items(answers[kind]), 1)]
    rules = [dict(id="scope-{}".format(index), text=text, enforcement="hard")
             for index, text in enumerate(_items(answers["exclusions"]), 1)]
    rules.append(dict(AUTHORITY_RULE))
    agents = [dict(id="box-{}".format(index), role="Implement and evidence the approved task", box="camol-boxes/box-{}".format(index),
                   capabilities=["code", "inspect", "test"], adapter=copy.deepcopy(adapter), trust_tier=tier)
              for index in range(1, limits["boxes"] + 1)]
    network = adapter.get("profile_snapshot", {}).get("network_destinations", [])
    envelope = dict(schema="camol.creation_envelope", schema_version=1, run_id=state["run_id"],
                    goal=state["goal"], owner=state["owner"], source=state["source"],
                    planning_model=state["planning_model"], effort=state["effort"], requirements=requirements,
                    exclusions=_items(answers["exclusions"]), oracle_guidance=answers["oracles"],
                    clarifications=copy.deepcopy(state["clarifications"]), limits=limits, agents=agents, rules=rules,
                    run=dict(id=state["run_id"], objective=state["goal"], max_concurrency=limits["concurrency"],
                             completion=list(COMPLETION),
                             token_policy=dict(max_tokens_per_turn=min(8000, limits["tokens"], adapter.get("profile_snapshot", {}).get("max_turn_tokens", 8000)), checkpoint_reserve=600,
                                               max_total_tokens=limits["tokens"], max_turns_per_task=limits["turns"]),
                             readiness_policy=dict(receipt_ttl_seconds=300)),
                    scope_policy=dict(external_actions="not_authorized", worker_tier=tier,
                                      network="unrestricted" if network else "denied_by_policy",
                                      enforcement="os_scoped" if tier == "developer_sandboxed" else "behavioral_only_unenforced"))
    return validate_creation_envelope(envelope)


def validate_creation_envelope(value):
    fields = {"schema", "schema_version", "run_id", "goal", "owner", "source", "planning_model", "effort",
              "requirements", "exclusions", "oracle_guidance", "clarifications", "limits", "agents", "rules", "run", "scope_policy"}
    if not isinstance(value, dict) or set(value) != fields or value["schema"] != "camol.creation_envelope" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PlanningError("creation envelope has missing or unknown fields")
    from .debug_execution import validate_source
    validate_source(value["source"])
    for field in ("run_id", "owner"):
        require_identifier(value[field], "creation " + field)
    for field in ("goal", "planning_model", "oracle_guidance"):
        _required_text(value[field], "creation " + field, 4000)
    from .conversation import parse_selection
    parse_selection(value["planning_model"])
    if not isinstance(value["effort"], str) or value["effort"] not in {"low", "medium", "high", "xhigh", "max"}:
        raise PlanningError("creation effort is invalid")
    _clarifications(value["clarifications"])
    limits = value["limits"]
    if not isinstance(limits, dict) or set(limits) != set(BOUNDS):
        raise PlanningError("creation limits have missing or unknown fields")
    if any(type(limits[key]) is not int or not lower <= limits[key] <= upper for key, (lower, upper) in BOUNDS.items()) or limits["concurrency"] > limits["boxes"]:
        raise PlanningError("creation limits exceed their supported bounds")
    if not isinstance(value["requirements"], list) or not 2 <= len(value["requirements"]) <= 32:
        raise PlanningError("creation requirements must be bounded outcomes and invariants")
    counters = {"outcomes": 0, "invariants": 0}
    for item in value["requirements"]:
        if not isinstance(item, dict) or set(item) != {"requirement_id", "kind", "text"} or not isinstance(item["kind"], str) or item["kind"] not in counters:
            raise PlanningError("creation requirement has missing or unknown fields")
        counters[item["kind"]] += 1
        if item["requirement_id"] != "{}-{}".format(item["kind"], counters[item["kind"]]):
            raise PlanningError("creation requirement identity is invalid")
        _required_text(item["text"], "creation requirement", 2000)
    if not all(counters.values()):
        raise PlanningError("creation needs both observable outcomes and invariants")
    if not isinstance(value["exclusions"], list) or not value["exclusions"]:
        raise PlanningError("creation exclusions must be explicit")
    for item in value["exclusions"]:
        _required_text(item, "creation exclusion", 2000)
    expected_rules = [dict(id="scope-{}".format(i), text=text, enforcement="hard") for i, text in enumerate(value["exclusions"], 1)] + [dict(AUTHORITY_RULE)]
    if value["rules"] != expected_rules:
        raise PlanningError("creation rules differ from the human scope")
    agents = value["agents"]
    if not isinstance(agents, list) or len(agents) != limits["boxes"] or not isinstance(agents[0], dict):
        raise PlanningError("creation box pool differs from reviewed limits")
    adapter, tier = agents[0].get("adapter"), agents[0].get("trust_tier")
    if not isinstance(adapter, dict) or tier not in {"developer_trusted", "developer_sandboxed"}:
        raise PlanningError("creation worker adapter or trust tier is invalid")
    expected_agents = [dict(id="box-{}".format(i), role="Implement and evidence the approved task", box="camol-boxes/box-{}".format(i),
                           capabilities=["code", "inspect", "test"], adapter=adapter, trust_tier=tier) for i in range(1, limits["boxes"] + 1)]
    if agents != expected_agents or adapter.get("timeout_seconds") != limits["timeout"]:
        raise PlanningError("creation agents differ from the exact homogeneous worker policy")
    profile = None
    if adapter.get("kind") == "process":
        if limits["cost_cents"] != 0:
            raise PlanningError("process cost cannot be represented as a provider measurement")
    else:
        profile = ModelProfile.from_dict(adapter.get("profile_snapshot"))
        if (profile.adapter_kind != adapter.get("kind") or profile.effort != value["effort"]
                or profile.max_run_usd_cents != limits["cost_cents"] or profile.max_agent_turns > limits["turns"]
                or profile.max_turn_tokens > min(limits["tokens"], 8000) or tier != "developer_trusted"):
            raise PlanningError("creation provider profile exceeds or conflicts with reviewed controls")
    expected_run = dict(id=value["run_id"], objective=value["goal"], max_concurrency=limits["concurrency"], completion=list(COMPLETION),
                        token_policy=dict(max_tokens_per_turn=min(8000, limits["tokens"], profile.max_turn_tokens if profile else 8000),
                                          checkpoint_reserve=600, max_total_tokens=limits["tokens"], max_turns_per_task=limits["turns"]),
                        readiness_policy=dict(receipt_ttl_seconds=300))
    if value["run"] != expected_run:
        raise PlanningError("creation run controls differ from the human-reviewed limits")
    expected_scope = dict(external_actions="not_authorized", worker_tier=tier,
                          network="unrestricted" if profile and profile.network_destinations else "denied_by_policy",
                          enforcement="os_scoped" if tier == "developer_sandboxed" else "behavioral_only_unenforced")
    if value["scope_policy"] != expected_scope:
        raise PlanningError("creation scope claims exceed the selected runtime guarantees")
    # Validate the kernel control/profile shapes without fabricating any V5
    # invariant or executable candidate. This temporary data is never run.
    validate_runbook(dict(schema_version=4, run=value["run"], agents=agents, rules=value["rules"], tasks=[
        dict(id="shape-validation", goal="schema only", depends_on=[], capabilities=["code"], acceptance=["schema only"],
             required_evidence=["test_result"], max_attempts=1, evaluator_assets=[],
             steps=[dict(id="shape", instruction="schema only", commands=[], completion=["schema only"])],
             verification=[dict(purpose="schema only", argv=["python3", "-c", "pass"])])]))
    reject_sensitive_text(json.dumps(value), "creation envelope")
    if not value["requirements"] or not value["agents"] or not value["rules"]:
        raise PlanningError("creation envelope requires reviewed intent and worker selection")
    if len(json.dumps(value)) > 14000:
        raise PlanningError("creation envelope is too large; narrow the goal")
    return copy.deepcopy(value)


def render_envelope(envelope):
    envelope = validate_creation_envelope(envelope)
    return ("DRAFT CREATION ENVELOPE " + canonical_digest(envelope) +
            "\nThis confirms planning boundaries only, not worker execution. Profiles and limits below are exact.\n" +
            "Network/isolation: " + json.dumps(envelope["scope_policy"], sort_keys=True) +
            "\nBehavioral scope restrictions are NOT hard egress protection on the weaker host tier; hosted inference needs network.\n" +
            json.dumps(envelope, indent=2, sort_keys=True) +
            "\nReview then /draft confirm " + canonical_digest(envelope) + "; only then explicitly /propose.")


def creation_prompt(envelope):
    envelope = validate_creation_envelope(envelope)
    instructions = """Propose a complete UNAPPROVED Camol schema_version 5 runbook from human intent.
Return ONLY {"questions":["specific oracle/scope question"]} OR exactly
{"runbook":FULL_V5_RUNBOOK,"coverage":[{"requirement_id":"outcomes-1","invariant_ids":["id"],"rationale":"how the proposed check could falsify this exact requirement"}],"unresolved_questions":[]}.
Unknown observable signals or oracle mappings must return questions; do not invent established measurements or claim an existing test exists.
Copy ENVELOPE.run, agents and rules exactly. New tasks and command argv may be proposed, never executed.
Stay within max tasks, per-task attempts, total attempts=tasks*attempts, 64 total steps, 128 work commands and 64 verification commands. No new profiles, trust, capabilities or external authority.
Each task has id,goal,depends_on,capabilities,acceptance,required_evidence,max_attempts,steps,verification,evaluator_assets.
steps have id,instruction,commands:[{purpose,argv}],completion. verification have purpose,argv and optional cwd=workspace_root. evaluator_assets name only existing frozen source files (inline verification is permitted).
Every task requires command,artifact,claim,test_result evidence and an independent verification command; no patch-integrity-only success substituted for behavioral verification.
state_model has invariants,obligations,gates,final_acceptance:'human'. Every invariant is owner-owned with approval_policy:'human',positive_evidence_required:true,nonempty falsification strategies and test_result evidence.
For EVERY human requirement, coverage must map at least one invariant whose predicate is EXACTLY that requirement.text. Additional local invariants may refine it, never replace it. Coverage is proposed rationale, not verified proof.
The exact human mapping has preconditions:[]; outcome requirements use modality:'eventually'. Requirements of kind invariants use scope:'global',modality:'preserved', protecting every task gate; never narrow them to one task or a conditional predicate.
Invariant fields: schema:'camol.invariant',schema_version:1,invariant_id,revision:1,owner,scope,modality,predicate,preconditions:[],observables:[text],severity:'normal',falsification_strategies:[text],required_evidence:['test_result'],positive_evidence_required:true,approval_policy:'human'.
Obligation fields: schema:'camol.obligation',schema_version:1,obligation_id,owner,target_state:'ACCEPTED',invariant_ids,task_ids.
Gates explicitly bind task_id,policy,invariant_ids,obligation_ids,evaluators:[{verification_index:0,family:'deterministic',invariant_ids:[id]}]. Every evaluator must map an invariant. All policies require human approval. Use the supplied POLICY shape with distinct policy_id. Final acceptance is human.
If any requirement is not testable with the proposed scope, return questions instead of a candidate.
"""
    from .gates import GatePolicy
    policy = GatePolicy.compile("proposed-gate", "basic").to_dict()
    policy["human_approval"] = True
    prompt = instructions + json.dumps(dict(ENVELOPE=envelope, POLICY=policy), sort_keys=True, separators=(",", ":"))
    if len(prompt) > MAX_PROMPT_CHARS:
        raise PlanningError("creation request exceeds the complete planning-message limit; narrow the envelope")
    return prompt


def parse_creation_response(text, envelope):
    # The model supplies arbitrary JSON, including wrong container types at
    # nested kernel enum fields. Normalize only here at the untrusted-response
    # boundary so a malformed candidate cannot crash the interactive client.
    try:
        return _parse_creation_response(text, envelope)
    except (TypeError, KeyError, AttributeError, IndexError) as error:
        raise PlanningError("creation response contains malformed nested contract data") from error


def _parse_creation_response(text, envelope):
    if not isinstance(text, str) or len(text) > MAX_RESPONSE_CHARS:
        raise PlanningError("creation response exceeds the candidate bound")
    reject_sensitive_text(text, "creation response")
    response = strict_json(text)
    if not isinstance(response, dict):
        raise PlanningError("creation response must be an object")
    if set(response) == {"questions"}:
        questions = response["questions"]
    elif set(response) == {"runbook", "coverage", "unresolved_questions"}:
        questions = response["unresolved_questions"]
    else:
        raise PlanningError("creation response needs exactly questions, or runbook/coverage/unresolved_questions")
    if not isinstance(questions, list) or len(questions) > 8 or any(not isinstance(q, str) or not q.strip() or len(q) > 1500 for q in questions):
        raise PlanningError("creation questions must be bounded non-empty text")
    if questions:
        return {"questions": questions}
    if "runbook" not in response:
        raise PlanningError("creation questions cannot be empty")
    runbook = validate_created_runbook(response["runbook"], envelope)
    coverage = response["coverage"]
    if not isinstance(coverage, list):
        raise PlanningError("candidate coverage must be explicit")
    requirements = {r["requirement_id"]: r for r in envelope["requirements"]}
    invariants = {i["invariant_id"]: i for i in runbook["state_model"]["invariants"]}
    seen = set()
    for item in coverage:
        if (not isinstance(item, dict) or set(item) != {"requirement_id", "invariant_ids", "rationale"}
                or not isinstance(item["requirement_id"], str) or item["requirement_id"] not in requirements or item["requirement_id"] in seen):
            raise PlanningError("candidate coverage has invalid or duplicate requirement identity")
        seen.add(item["requirement_id"])
        ids = item["invariant_ids"]
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in invariants for i in ids):
            raise PlanningError("candidate coverage names missing invariants")
        _required_text(item["rationale"], "coverage rationale", 2000)
        requirement = requirements[item["requirement_id"]]
        exact = [invariants[i] for i in ids if invariants[i]["predicate"] == requirement["text"]]
        if not exact:
            raise PlanningError("candidate replaced a human requirement rather than mapping its exact predicate")
        if not any(not invariant["preconditions"] and (
                invariant["scope"] == "global" and invariant["modality"] == "preserved"
                if requirement["kind"] == "invariants" else invariant["modality"] == "eventually") for invariant in exact):
            raise PlanningError("candidate narrowed the human requirement scope or modality, or added preconditions")
    if seen != set(requirements):
        return {"questions": ["Which independent observable check covers {}?".format(requirements[key]["text"]) for key in sorted(set(requirements) - seen)][:8]}
    return dict(runbook=runbook, coverage=coverage)


def validate_created_runbook(value, envelope):
    envelope = validate_creation_envelope(envelope)
    runbook = validate_runbook(value)
    if runbook["schema_version"] != 5 or any(runbook[field] != envelope[field] for field in ("run", "agents", "rules")):
        raise PlanningError("model changed frozen creation controls, profiles, rules or schema")
    tasks, limits = runbook["tasks"], envelope["limits"]
    if (len(tasks) > limits["tasks"] or sum(len(t["steps"]) for t in tasks) > 64
            or sum(len(s["commands"]) for t in tasks for s in t["steps"]) > 128
            or sum(len(t["verification"]) for t in tasks) > 64):
        raise PlanningError("candidate exceeds the reviewed task/step/command envelope")
    for task in tasks:
        if task["max_attempts"] > limits["attempts"] or not set(task["capabilities"]).issubset({"code", "inspect", "test"}):
            raise PlanningError("candidate increased task attempts or capabilities")
        if not {"command", "artifact", "claim", "test_result"}.issubset(task["required_evidence"]):
            raise PlanningError("candidate omitted mandatory task evidence")
    model = runbook["state_model"]
    for invariant in model["invariants"]:
        if (invariant["owner"] != envelope["owner"] or invariant["approval_policy"] != "human"
                or not invariant["positive_evidence_required"] or not invariant["falsification_strategies"]
                or "test_result" not in invariant["required_evidence"]):
            raise PlanningError("proposed invariant requires owner review, positive evidence and explicit falsification")
    if any(item["owner"] != envelope["owner"] for item in model["obligations"]):
        raise PlanningError("candidate obligations must remain human-owned")
    if any(not item["policy"]["human_approval"] for item in model["gates"]) or model["final_acceptance"] != "human":
        raise PlanningError("every proposed task and final outcome must require human acceptance")
    return runbook


def render_risk(plan, digest):
    origin = plan["origin"]
    envelope = origin["creation_envelope"]
    lines = ["DRAFT RISK / SCOPE REVIEW " + digest,
             "All commands below are newly proposed, UNEXECUTED code. Labels and coverage rationale are not safety or correctness evidence.",
             "External deployment/uploads/downloads/credential mutation are NOT authorized by this workflow.",
             "Actual scope policy: " + json.dumps(envelope["scope_policy"], sort_keys=True),
             "On developer_trusted/unrestricted profiles, no hard no-egress or host-write guarantee is claimed. Hosted inference itself needs network.",
             "Source: " + json.dumps(plan["source"], sort_keys=True),
             "Limits: " + json.dumps(envelope["limits"], sort_keys=True),
             "EXACT WORKER POLICIES / ARGV (not just the model-proposed task commands):", json.dumps(envelope["agents"], indent=2),
             "Human requirements:", json.dumps(envelope["requirements"], indent=2),
             "Explicit invariant/oracle coverage (human must challenge each mapping):", json.dumps(origin["coverage"], indent=2),
             "Full proposed invariants, scope, modality and evaluator bindings:", json.dumps(plan["runbook"]["state_model"], indent=2)]
    for task in plan["runbook"]["tasks"]:
        lines.append("TASK {} <- {}".format(task["id"], ", ".join(task["depends_on"]) or "none"))
        for step in task["steps"]:
            for command in step["commands"]:
                lines.append("  PROPOSED WORK argv=" + json.dumps(command["argv"]))
        for command in task["verification"]:
            lines.append("  PROPOSED EVALUATOR cwd={} argv={}".format(command.get("cwd", "task"), json.dumps(command["argv"])))
    lines.append("Opaque shell/interpreter programs have their full runtime authority; command-name screening is not an isolation boundary.")
    lines.append("If acceptable, acknowledge this exact scope with /review " + digest + ", then separately /approve " + digest)
    return "\n".join(lines)
