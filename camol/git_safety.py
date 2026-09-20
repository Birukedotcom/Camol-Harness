"""Git plumbing with repository-configured executable callbacks disabled."""

import os
import re
import subprocess


class GitSafetyError(ValueError):
    pass


GIT_SAFETY_ARGS = (
    "--no-replace-objects",
    "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
    "-c", "core.ignoreStat=false", "-c", "core.trustctime=true", "-c", "core.checkStat=default",
    "-c", "core.hooksPath=" + os.devnull, "-c", "core.sshCommand=false",
    "-c", "core.pager=cat", "-c", "protocol.allow=never",
    "-c", "submodule.recurse=false", "-c", "fetch.recurseSubmodules=false",
    "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false",
    "-c", "credential.helper=", "-c", "core.editor=false", "-c", "sequence.editor=false",
)


def safe_git_argv(binary, arguments, *, env, cwd=None, timeout=20):
    """Observe callback names without execution, then override every driver.

    Keys are queried with a NUL delimiter, never interpolated into a shell.
    Unsupported/hostile configuration fails closed. Filters must be disabled
    for status/hash/index updates as well as checkout: --no-ext-diff alone is
    insufficient because a clean/process filter participates in content hashing.
    """
    args = list(arguments)
    prefix, index = [], 0
    while index < len(args) and args[index].startswith("-"):
        item = args[index]
        if item in {"--version", "--help"}:
            return [binary, *GIT_SAFETY_ARGS, *args]
        if item in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}:
            if index + 1 >= len(args):
                raise GitSafetyError("Git global option is missing its argument")
            prefix.extend(args[index:index + 2])
            index += 2
        elif item in {"--no-pager", "--no-optional-locks", "--no-replace-objects", "--bare"} or item.startswith(("--git-dir=", "--work-tree=")):
            prefix.append(item)
            index += 1
        else:
            raise GitSafetyError("unsupported Git global option in trusted plumbing")
    if index >= len(args):
        raise GitSafetyError("Git plumbing requires a command")
    command, tail = args[index], args[index + 1:]
    base = [binary, *prefix, *GIT_SAFETY_ARGS, "--no-pager"]
    # Config reads and object/index listings never convert worktree content.
    # They cannot invoke a clean/smudge, textconv or external diff callback.
    pure = {"config", "rev-parse", "ls-files", "ls-tree", "symbolic-ref", "show-ref", "check-ref-format"}
    if command == "remote" and tail and tail[0] == "get-url":
        pure.add("remote")
    if command == "cat-file" and not any(value in {"--filters", "--textconv"} for value in tail):
        pure.add("cat-file")
    if command in pure:
        return [*base, command, *tail]
    try:
        observed = subprocess.run(
            [*base, "config", "--null", "--name-only", "--get-regexp", r"^(filter\..*\.(clean|smudge|process|required)|diff\..*\.(command|textconv))$"],
            cwd=str(cwd) if cwd is not None else None, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=min(timeout, 20), check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitSafetyError("Git callback configuration could not be inspected") from error
    if observed.returncode not in {0, 1} or len(observed.stdout) > 1 << 20:
        raise GitSafetyError("Git callback configuration is invalid or exceeds the safety bound")
    try:
        keys = observed.stdout.decode("utf-8", "strict").split("\0")
    except UnicodeError as error:
        raise GitSafetyError("Git callback keys must be valid UTF-8") from error
    if len(keys) > 4096:
        raise GitSafetyError("Git callback configuration has too many keys")
    filters, diffs = set(), set()
    for key in keys:
        if not key:
            continue
        match = re.fullmatch(r"(filter|diff)\.([A-Za-z0-9_./:-]+)\.(clean|smudge|process|required|command|textconv)", key)
        if not match:
            raise GitSafetyError("Git callback configuration contains an unsupported driver name")
        (filters if match[1] == "filter" else diffs).add(match[2])
    for name in sorted(filters):
        for field in ("clean", "smudge", "process", "required"):
            base += ["-c", "filter.{}.{}={}".format(name, field, "false" if field == "required" else "")]
    for name in sorted(diffs):
        base += ["-c", "diff.{}.command=".format(name), "-c", "diff.{}.textconv=".format(name)]
    if command in {"status", "diff"}:
        # Cached index opt-outs can suppress real byte changes even with a
        # same-sized file and restored mtime. A masked checkout is not proof
        # of cleanliness; do not silently rewrite the owner's index flags.
        flags = subprocess.run([*base, "ls-files", "-v", "-z"], cwd=str(cwd) if cwd is not None else None,
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=min(timeout, 20), check=False)
        if flags.returncode or len(flags.stdout) > 32 << 20:
            raise GitSafetyError("Git index visibility could not be inspected")
        if any(entry and (entry[0] == ord("S") or ord("a") <= entry[0] <= ord("z")) for entry in flags.stdout.split(b"\0")):
            raise GitSafetyError("Git assume-unchanged/skip-worktree flags hide source changes; clear them explicitly before using this checkout")
    if command in {"diff", "show", "log"}:
        tail = ["--no-ext-diff", "--no-textconv", *tail]
    return [*base, command, *tail]
