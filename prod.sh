#!/usr/bin/env bash
# prod.sh — tracing.run prod orchestrator.
#
# Two flows:
#   build (in dev worktree)        — build + smoke + tag image (no deploy)
#   deploy / rollback / status     — operate on $PROD_ROOT/<name>/
#
# Multi-instance design: each deploy lives in its own dir under
# $PROD_ROOT (default ~/tracing-run-prod), one per user.
# Adding a new instance = mkdir + drop a docker-compose.yml + .env, no
# script edit needed.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="tracing-run"
PROD_ROOT="${PROD_ROOT:-$HOME/tracing-run-prod}"

cd "$REPO_DIR"

# ── Output helpers ──────────────────────────────────────────────────────────
fail()    { printf '\033[0;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
info()    { printf '\033[0;36m→\033[0m %s\n' "$*"; }
success() { printf '\033[0;32m✓\033[0m %s\n' "$*"; }
warn()    { printf '\033[0;33m⚠\033[0m %s\n' "$*"; }
confirm() {
    read -r -p "$1 [y/N] " r
    [[ "$r" == "y" || "$r" == "Y" ]] || { warn "Aborted."; exit 0; }
}

# ── Branch → namespace mapping ──────────────────────────────────────────────
# main → no namespace      → tag git as vX.Y.Z, image as :latest-main + :vX.Y.Z
# activity-only → namespace → tag git as activity-only/vX.Y.Z,
#                              image as :latest-activity-only + :activity-only-vX.Y.Z
detect_branch() {
    git branch --show-current 2>/dev/null \
        || fail "Not a git repo (or detached HEAD)? Couldn't detect branch."
}

derive_latest_tag() {
    case "$1" in
        main)          echo "latest-main" ;;
        activity-only) echo "latest-activity-only" ;;
        *) fail "Unsupported branch: $1 (expected main or activity-only)" ;;
    esac
}

image_id() { docker images --no-trunc -q "${IMAGE_NAME}:$1" 2>/dev/null | head -1; }

# AST-based smoke test inside the freshly-built image. Walks every .py in
# /app, collects every imported top-level module, then tries to load each
# inside the container's python. Catches "added new .py but forgot to COPY
# in the Dockerfile" + "added a dependency but forgot requirements.txt".
smoke_test() {
    local latest_tag="$1"
    info "Running AST import smoke test on ${IMAGE_NAME}:${latest_tag}…"
    docker run --rm -i "${IMAGE_NAME}:${latest_tag}" python3 - <<'PYEOF'
import ast, pathlib, importlib, sys

imported = set()
parse_errors = []
for f in sorted(pathlib.Path("/app").rglob("*.py")):
    if "__pycache__" in str(f):
        continue
    try:
        tree = ast.parse(f.read_text(), filename=str(f))
    except SyntaxError as e:
        parse_errors.append(f"  {f}: {e}")
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imported.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])

import_errors = []
for mod in sorted(imported):
    try:
        importlib.import_module(mod)
    except Exception as e:
        import_errors.append(f"  {mod}: {type(e).__name__}: {e}")

if parse_errors or import_errors:
    if parse_errors:
        print("Syntax errors:", file=sys.stderr)
        print("\n".join(parse_errors), file=sys.stderr)
    if import_errors:
        print("Import errors (referenced in source but cannot be loaded "
              "— likely missing from Dockerfile COPY or requirements.txt):",
              file=sys.stderr)
        print("\n".join(import_errors), file=sys.stderr)
    sys.exit(1)

print(f"Smoke OK — {len(imported)} top-level modules resolve across source tree")
PYEOF
}

# Publish a GitHub Release for an already-pushed tag. A git tag and a GitHub
# Release are different objects — `git push <tag>` creates only the former, so
# this fills in the latter to match the v1.0.x releases on the repo.
#
# Deliberately non-fatal: by the time this runs the image is built and the tag
# is pushed, so a missing release is cosmetic. Missing/unauthenticated gh, or
# an already-existing release, just warn and return 0 — never abort the build.
create_github_release() {
    local tag="$1" message="$2"
    if ! command -v gh >/dev/null 2>&1; then
        warn "gh CLI not found — skipping GitHub Release. Create later: gh release create $tag"
        return 0
    fi
    if ! gh auth status >/dev/null 2>&1; then
        warn "gh not authenticated — skipping GitHub Release. Run 'gh auth login', then: gh release create $tag"
        return 0
    fi
    if gh release view "$tag" >/dev/null 2>&1; then
        warn "GitHub Release $tag already exists — leaving it untouched."
        return 0
    fi
    info "Publishing GitHub Release: $tag"
    if gh release create "$tag" --title "$tag — $message" --notes "$message"; then
        success "Published GitHub Release $tag"
    else
        warn "GitHub Release $tag failed — create manually: gh release create $tag --title \"$tag — $message\""
    fi
}

# ── build ────────────────────────────────────────────────────────────────────
# Builds the image, smoke-tests it, tags git + image. Does NOT deploy to any
# prod container — that's a separate explicit step (`deploy <name>`).
cmd_build() {
    local version="${1:-}" message="${2:-}"
    [[ -z "$version" || -z "$message" ]] && fail "Usage: prod.sh build <version> <message>"
    [[ ! "$version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] \
        && fail "Version must match vX.Y.Z (got: $version). Don't include namespace — auto-derived from branch."

    local branch latest_tag full_version image_version
    branch=$(detect_branch)
    latest_tag=$(derive_latest_tag "$branch")
    if [[ "$branch" == "main" ]]; then
        full_version="$version"
    else
        full_version="${branch}/${version}"
    fi
    image_version="${full_version//\//-}"  # docker tags can't contain "/"

    [[ -n "$(git status --porcelain)" ]] && fail "Uncommitted changes — commit or stash first."
    git rev-parse -q --verify "refs/tags/$full_version" >/dev/null 2>&1 \
        && fail "Tag $full_version already exists locally."
    git ls-remote --tags origin "refs/tags/$full_version" 2>/dev/null | grep -q "$full_version" \
        && fail "Tag $full_version already exists on remote."

    local version_no_v="${version#v}"
    local commit; commit=$(git rev-parse --short HEAD)
    info "Branch:    $branch"
    info "Version:   $full_version"
    info "Image:     ${IMAGE_NAME}:${latest_tag} + ${IMAGE_NAME}:${image_version}"
    info "Commit:    $commit"
    info "Message:   $message"
    confirm "Proceed?"

    # Bump the VERSION file + make a release commit so the running container
    # reports the tag it shipped as (read at app startup by _read_version()).
    # Tag goes on the bump commit; both the file content and the git tag are
    # synced automatically going forward.
    if [[ ! -f VERSION ]] || [[ "$(cat VERSION)" != "$version_no_v" ]]; then
        info "Writing VERSION → $version_no_v"
        echo "$version_no_v" > VERSION
        git add VERSION
        git commit -m "Release $full_version" >/dev/null
        commit=$(git rev-parse --short HEAD)
    fi

    # Snapshot the soon-to-be-overwritten :latest-<branch> as a rollback alias.
    # We figure out which version the current latest-* tag points at by SHA
    # match, then stash a copy as :rollback-<that-version> for prod.sh rollback.
    local old_latest_sha
    old_latest_sha=$(docker inspect "${IMAGE_NAME}:${latest_tag}" --format '{{.Id}}' 2>/dev/null || echo "")
    if [[ -n "$old_latest_sha" ]]; then
        local old_tagged
        old_tagged=$(docker images "${IMAGE_NAME}" --format '{{.Tag}} {{.ID}}' \
            | awk -v id="${old_latest_sha#sha256:}" -v skip="$latest_tag" \
                  'substr(id, 1, 12) == $2 && $1 != skip {print $1; exit}')
        if [[ -n "$old_tagged" ]]; then
            info "Backup: ${IMAGE_NAME}:${latest_tag} → ${IMAGE_NAME}:rollback-${old_tagged}"
            docker tag "${IMAGE_NAME}:${latest_tag}" "${IMAGE_NAME}:rollback-${old_tagged}"
        fi
    fi

    info "Building image as ${IMAGE_NAME}:${latest_tag}…"
    # Use `docker build` directly (not `docker compose build`) so we don't
    # have to satisfy the compose file's runtime env interpolation (it has
    # `${LOGIN_PASSWORD:?…}` which fails the build). Compose is for runtime;
    # for image building, plain docker build is the right tool.
    docker build -t "${IMAGE_NAME}:${latest_tag}" .

    if ! smoke_test "$latest_tag"; then
        if [[ -n "$old_latest_sha" ]]; then
            warn "Restoring ${IMAGE_NAME}:${latest_tag} to previous image"
            docker tag "$old_latest_sha" "${IMAGE_NAME}:${latest_tag}"
        fi
        fail "Smoke test failed — git tag NOT pushed, no prod containers touched."
    fi

    info "Tagging git: $full_version"
    git tag -a "$full_version" -m "$message"
    git push origin "$full_version"

    # Tag exists on remote now — publish the matching GitHub Release (non-fatal).
    create_github_release "$full_version" "$message"

    info "Tagging image: ${IMAGE_NAME}:${image_version}"
    docker tag "${IMAGE_NAME}:${latest_tag}" "${IMAGE_NAME}:${image_version}"

    success "Built $full_version"
    info "Next:  ./prod.sh deploy <name>     # to put this image into a prod container"
    info "       ./prod.sh status            # to see all prods"
}

# ── deploy ──────────────────────────────────────────────────────────────────
# Recreates the named prod container with whatever image its compose file
# references (typically :latest-main or :latest-activity-only). Picks up
# changes that `build` made to those floating tags.
cmd_deploy() {
    local name="${1:-}"
    [[ -z "$name" ]] && fail "Usage: prod.sh deploy <name>   (must match a directory under \$PROD_ROOT)"
    local prod_dir="${PROD_ROOT}/${name}"
    [[ ! -d "$prod_dir" ]] && fail "Prod dir $prod_dir does not exist. Create it (mkdir + docker-compose.yml + .env) first."
    [[ ! -f "$prod_dir/docker-compose.yml" ]] && fail "$prod_dir/docker-compose.yml missing."
    [[ ! -f "$prod_dir/.env" ]] && fail "$prod_dir/.env missing — instance config not set up."

    info "Deploying $name (from $prod_dir)…"
    (cd "$prod_dir" && docker compose up -d --force-recreate)
    sleep 3
    success "Deployed $name"
    (cd "$prod_dir" && docker compose ps)
}

# ── rollback ────────────────────────────────────────────────────────────────
# Repoints the floating :latest-<branch> tag back to a previous version, then
# recreates the named container so it picks up the old image. Leaves the
# version-specific image tag (eg :v1.0.2) alone for future reference.
cmd_rollback() {
    local name="${1:-}" version="${2:-}"
    [[ -z "$name" || -z "$version" ]] && fail "Usage: prod.sh rollback <name> <version>"

    local image_version="${version//\//-}"
    local latest_tag
    if [[ "$version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        latest_tag="latest-main"
    elif [[ "$version" =~ ^([a-z][a-z0-9_-]*)/v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        latest_tag="latest-${BASH_REMATCH[1]}"
    else
        fail "Version must match vX.Y.Z or <branch>/vX.Y.Z (got: $version)"
    fi

    [[ -z "$(image_id "$image_version")" ]] \
        && fail "Image ${IMAGE_NAME}:${image_version} not found locally. Try 'prod.sh list'."

    local prod_dir="${PROD_ROOT}/${name}"
    [[ ! -d "$prod_dir" ]] && fail "Prod dir $prod_dir does not exist."

    info "Rollback $name → $version (will retag ${IMAGE_NAME}:${latest_tag})"
    confirm "Proceed?"

    # Snapshot the about-to-be-replaced :latest-<branch> for rollback-of-rollback.
    local cur_latest_sha
    cur_latest_sha=$(docker inspect "${IMAGE_NAME}:${latest_tag}" --format '{{.Id}}' 2>/dev/null || echo "")
    if [[ -n "$cur_latest_sha" ]]; then
        local cur_tagged
        cur_tagged=$(docker images "${IMAGE_NAME}" --format '{{.Tag}} {{.ID}}' \
            | awk -v id="${cur_latest_sha#sha256:}" -v skip="$latest_tag" \
                  'substr(id, 1, 12) == $2 && $1 != skip {print $1; exit}')
        if [[ -n "$cur_tagged" ]]; then
            info "Backup: ${IMAGE_NAME}:${latest_tag} → ${IMAGE_NAME}:rollback-from-${cur_tagged}"
            docker tag "${IMAGE_NAME}:${latest_tag}" "${IMAGE_NAME}:rollback-from-${cur_tagged}"
        fi
    fi

    info "Retag: ${IMAGE_NAME}:${latest_tag} → image of ${IMAGE_NAME}:${image_version}"
    docker tag "${IMAGE_NAME}:${image_version}" "${IMAGE_NAME}:${latest_tag}"

    info "Recreating $name container…"
    (cd "$prod_dir" && docker compose up -d --force-recreate)
    sleep 3
    success "Rolled back $name to $version"
    (cd "$prod_dir" && docker compose ps)
}

# ── status ──────────────────────────────────────────────────────────────────
cmd_status() {
    local name="${1:-}"
    if [[ -n "$name" ]]; then
        local prod_dir="${PROD_ROOT}/${name}"
        [[ ! -d "$prod_dir" ]] && fail "Prod dir $prod_dir does not exist."
        info "$name  ($prod_dir)"
        (cd "$prod_dir" && docker compose ps)
        return
    fi

    if [[ ! -d "$PROD_ROOT" ]]; then
        warn "$PROD_ROOT does not exist."
        return
    fi
    info "All prod instances under $PROD_ROOT:"
    local found=0
    for d in "$PROD_ROOT"/*/; do
        [[ -d "$d" ]] || continue
        found=1
        local n; n=$(basename "$d")
        echo
        info "$n  ($d)"
        (cd "$d" && docker compose ps 2>/dev/null) || warn "  (compose ps failed for $n)"
    done
    [[ $found -eq 0 ]] && warn "No prod instances found in $PROD_ROOT."
}

# ── list ────────────────────────────────────────────────────────────────────
cmd_list() {
    info "Git tags (newest first, all branches):"
    local tags
    tags=$(git tag -l 'v*' '*/v*' --sort=-v:refname 2>/dev/null)
    if [[ -n "$tags" ]]; then
        while read -r t; do
            local commit msg
            commit=$(git rev-list -n 1 "$t" 2>/dev/null | cut -c1-7)
            msg=$(git tag -l --format='%(contents:subject)' "$t")
            printf "  %-26s %s  %s\n" "$t" "$commit" "$msg"
        done <<< "$tags"
    else
        echo "  (none)"
    fi

    echo ""
    info "Docker images (${IMAGE_NAME}):"
    docker images "${IMAGE_NAME}" --format 'table {{.Tag}}\t{{.ID}}\t{{.CreatedSince}}'
}

# ── Dispatch ────────────────────────────────────────────────────────────────
case "${1:-}" in
    build)    shift; cmd_build "$@"    ;;
    deploy)   shift; cmd_deploy "$@"   ;;
    rollback) shift; cmd_rollback "$@" ;;
    status)   shift; cmd_status "$@"   ;;
    list)     cmd_list                 ;;
    *)
        avail=$(ls "$PROD_ROOT" 2>/dev/null | tr '\n' ' ')
        cat <<EOF
Usage: ./prod.sh <subcommand> [args]

Build (in dev worktree):
  build <version> <message>     Build + smoke + tag git/image (NO deploy)
                                Auto-derives namespace from branch:
                                  main          → vX.Y.Z
                                  activity-only → activity-only/vX.Y.Z
                                e.g. ./prod.sh build v1.0.4 "fix mobile UX"

Deploy / manage (operates on \$PROD_ROOT/<name>/):
  deploy <name>                 Pull image, recreate container
                                e.g. ./prod.sh deploy alice
  rollback <name> <version>     Switch <name> back to a previous version
                                e.g. ./prod.sh rollback alice v1.0.2
  status [name]                 Show all prods (or a single one)
  list                          Show git tags + docker images

PROD_ROOT:    $PROD_ROOT
Available <name>: ${avail:-(none)}
EOF
        exit 1
        ;;
esac
