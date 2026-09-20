"""Bounded single-ref Git publication from an isolated object-only repository."""

import base64
import os
from pathlib import Path
import tempfile
import time
import stat

from .github_vcs import branch, repository, sha
from .git_safety import GIT_SAFETY_ARGS
from .preflight_process import bounded_preflight_run
from .recovery import _Git, MAX_BUNDLE
from .vcs import VCSError


def target(value):
    if not isinstance(value, dict) or set(value) != {"kind", "repository", "branch"}:
        raise VCSError("push target requires exactly kind, repository and branch")
    branch(value["branch"])
    if value["kind"] == "github_https":
        repository(value["repository"])
    elif value["kind"] == "local_bare":
        path = value["repository"]
        if not isinstance(path, str) or not Path(path).is_absolute() or str(Path(path)) != path or any(not c.isprintable() for c in path):
            raise VCSError("local push target requires a canonical absolute bare-repository path")
    else:
        raise VCSError("push transport must be github_https or local_bare")
    result = dict(value)
    if result["kind"] == "github_https":
        result["repository"] = result["repository"].lower()
    return result


def identity(value):
    value = target(value)
    if value["kind"] == "github_https":
        return dict(kind="github_repository", repository=value["repository"])
    path = Path(value["repository"])
    meta = path.stat()
    if path.resolve(strict=True) != path or not stat.S_ISDIR(meta.st_mode):
        raise VCSError("local push identity requires the exact nonlinked repository directory")
    return dict(kind="local_directory", device=meta.st_dev, inode=meta.st_ino)


def validate_identity(value, destination):
    if destination["kind"] == "github_https":
        if value != dict(kind="github_repository", repository=destination["repository"]):
            raise VCSError("push identity differs from its GitHub repository")
    elif (not isinstance(value, dict) or set(value) != {"kind", "device", "inode"}
            or value["kind"] != "local_directory" or any(type(value[k]) is not int or value[k] < 0 for k in ("device", "inode"))):
        raise VCSError("invalid local push directory identity")
    return dict(value)


def validate_credential(destination, token):
    if token is not None and (not isinstance(token, str) or not 8 <= len(token) <= 2048
                             or not token.isascii() or any(c.isspace() or not c.isprintable() for c in token)):
        raise VCSError("invalid explicitly supplied push credential")
    if destination["kind"] == "local_bare" and token is not None:
        raise VCSError("local publication does not accept a network credential")


def publish(proposal, *, token=None, cancel_event=None, authorize):
    """No retries, remote helpers, source config, checkout filters or source writes.

    Call only after a durable owner-approved intent. Even a nonzero push exit can
    leave remote effects; post-dispatch failures are deliberately unknown.
    """
    destination = target(proposal["target"])
    revision, previous = sha(proposal["binding"]["revision"]), proposal["expected_old"]
    if previous is not None:
        sha(previous)
    validate_credential(destination, token)
    started = time.monotonic()
    dispatched, before, after, exit_code = False, None, None, None
    result = dict(status="not_dispatched", dispatch_attempted=False, observed_before=None,
                  observed_after=None, push_exit_code=None, error_code="PUSH_NOT_DISPATCHED")
    try:
        with tempfile.TemporaryDirectory(prefix="camol-push-") as temporary:
            scratch = Path(temporary)
            git = _Git(scratch)
            git.deadline = started + proposal["timeout_seconds"]
            source = Path(proposal["source_workspace"])
            if source.resolve(strict=True) != source:
                raise VCSError("push source path changed or traverses a link")
            if git.call(source, "rev-parse", revision + "^{commit}").decode().strip() != revision:
                raise VCSError("push requires the exact integrated commit")
            if git.call(source, "rev-parse", "--is-shallow-repository").strip() != b"false":
                raise VCSError("push requires complete captured history")
            # Exact revision reachability only: no unrelated refs or dirty files.
            packed = git.call(source, "pack-objects", "--stdout", "--revs", "--no-reuse-delta",
                              data=(revision + "\n").encode(), maximum=MAX_BUNDLE)
            bare = scratch / "objects.git"
            bare.mkdir(mode=0o700)
            algorithm = "sha1" if len(revision) == 40 else "sha256"
            git.call(bare, "init", "--bare", "--quiet", "--template=", "--object-format=" + algorithm)
            git.call(bare, "index-pack", "--stdin", "--strict", data=packed)
            git.call(bare, "update-ref", "refs/heads/camol-publish", revision)
            if previous is not None and previous != revision:
                git.call(bare, "merge-base", "--is-ancestor", previous, revision)
            ref = "refs/heads/" + destination["branch"]
            env = dict(git.env, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/usr/bin/false", SSH_ASKPASS="/usr/bin/false")
            settings = ["-c", "http.followRedirects=false", "-c", "push.followTags=false"]
            if destination["kind"] == "github_https":
                url = "https://github.com/" + destination["repository"] + ".git"
                settings += ["-c", "protocol.https.allow=always"]
                if token is not None:
                    # Credential goes only to this child environment, never argv,
                    # repository config, proposal, receipt or retained output.
                    encoded = base64.b64encode(("x-access-token:" + token).encode()).decode()
                    env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                               GIT_CONFIG_VALUE_0="Authorization: Basic " + encoded)
            else:
                path = Path(destination["repository"])
                if identity(destination) != proposal["target_identity"] or git.call(path, "rev-parse", "--is-bare-repository").strip() != b"true":
                    raise VCSError("local push target is not the exact unlinked bare repository")
                url = str(path)
                settings += ["-c", "protocol.file.allow=always"]

            def call(*arguments):
                remaining = proposal["timeout_seconds"] - (time.monotonic() - started)
                if remaining <= 0:
                    raise VCSError("push operation deadline expired")
                return bounded_preflight_run([git.binary, *GIT_SAFETY_ARGS, *settings, "-C", str(bare), *arguments],
                    cwd=str(scratch), env=env, timeout=remaining, cancel_event=cancel_event, max_output_bytes=1 << 20)

            def read_ref():
                observed = call("ls-remote", "--refs", url, ref)
                if observed.returncode:
                    raise VCSError("push ref observation unavailable")
                lines = observed.stdout.splitlines()
                if not lines:
                    return None
                if len(lines) != 1:
                    raise VCSError("push ref observation ambiguous")
                oid, name = lines[0].decode("ascii").split("\t")
                if name != ref:
                    raise VCSError("push ref observation names another branch")
                return sha(oid)

            authorize()  # Recheck owner/plan before the first external read.
            if identity(destination) != proposal["target_identity"]:
                raise VCSError("push target identity changed before observation")
            before = read_ref()
            if before != previous:
                raise VCSError("push destination moved from the reviewed expected revision")
            authorize()  # Exact local authority is rechecked at the effect boundary.
            if identity(destination) != proposal["target_identity"]:
                raise VCSError("push target identity changed before dispatch")
            if before == revision:
                return dict(result, status="already_present", observed_before=before,
                            observed_after=before, error_code=None)
            dispatched = True
            pushed = call("push", "--porcelain", "--no-verify", "--no-follow-tags", "--recurse-submodules=no",
                          "--force-with-lease=" + ref + ":" + (previous or ""), url, revision + ":" + ref)
            exit_code = pushed.returncode
            if exit_code:
                raise VCSError("push outcome is not confirmed")
            after = read_ref()
            if after != revision or identity(destination) != proposal["target_identity"]:
                raise VCSError("push readback did not confirm the integrated revision")
            return dict(status="confirmed", dispatch_attempted=True, observed_before=before,
                        observed_after=after, push_exit_code=exit_code, error_code=None)
    except (Exception, KeyboardInterrupt):
        # No raw child text or exception argv is retained (it may contain secrets).
        return dict(result, status="effect_unknown" if dispatched else "not_dispatched",
                    dispatch_attempted=dispatched, observed_before=before, observed_after=after,
                    push_exit_code=exit_code, error_code="EFFECT_UNKNOWN" if dispatched else "PUSH_NOT_DISPATCHED")
