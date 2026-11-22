#!/usr/bin/env bash
#
# Build the production image for a release and push it. TAP-10322.
#
#   scripts/build-image.sh REPOSITORY TAG [COMMIT]
#   scripts/build-image.sh registry.example/savethedate v7
#   scripts/build-image.sh registry.example/savethedate v7 origin/main
#
# Run on nlt-dev, never on it13. it13 builds nothing: it pulls what this pushed, and
# `docker-compose.prod.yml` has no `build:` to fall back on.
#
# COMMIT defaults to the commit TAG names, so the usual order is to sign and push the
# tag and then run this. Give COMMIT to build before the tag exists. Either order
# works: the deploy unit on it13 refuses a tag whose image is not there yet, changes
# nothing, and looks again five minutes later.
#
# Three things the deploy relies on:
#
#   1. The image is built from the COMMIT, not from this working tree. `git archive`
#      is the build context, so an uncommitted edit or an untracked file cannot get
#      into a production image.
#   2. It carries that commit in the `org.opencontainers.image.revision` label.
#      `scripts/deploy_release.py` refuses an image whose label is not the commit the
#      signed tag names.
#   3. It is pushed as REPOSITORY:TAG. The deploy pulls that name, reads the digest
#      the registry gave it, and pins both in `image.env`.
#
# The last line printed is the pin, ready to be the whole of `image.env`. That is only
# needed by hand once, when the deploy directory is first set up; after that the
# deploy unit writes it.

set -euo pipefail

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  sed -n '3,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
fi
repository="$1"
tag="$2"
commitish="${3:-$tag}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# The same shape the deploy accepts: v1, v2, ... A tag it would never deploy is not
# worth pushing an image for.
if ! [[ "$tag" =~ ^v[0-9]+$ ]]; then
  echo "REFUSED: '$tag' is not a release tag. They are v1, v2, and so on." >&2
  exit 1
fi
case "$repository" in
  *@*|*[[:space:]]*|'')
    echo "REFUSED: '$repository' is not an image repository (no tag, no digest)." >&2
    exit 1
    ;;
esac

if ! commit="$(git rev-parse --verify --quiet "${commitish}^{commit}")"; then
  echo "REFUSED: '$commitish' is not a commit in this repository." >&2
  echo "If $tag is not tagged yet, name the commit to build: $0 $repository $tag origin/main" >&2
  exit 1
fi

reference="${repository}:${tag}"
echo "Building ${reference} from ${commit}."
git archive --format=tar "$commit" |
  docker build \
    --label "org.opencontainers.image.revision=${commit}" \
    --label "org.opencontainers.image.version=${tag}" \
    --tag "$reference" \
    -

docker push "$reference"

# The digest exists only once the registry has the image, which is why it is read
# back here and cannot be written into the compose file ahead of time.
#
# Tested with `if !`, not left to `set -e`. A failing command substitution in a plain
# assignment ends the script on the spot, with no message, and by this line the image
# is already in the registry. That has to be said.
if ! digests="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$reference")"; then
  echo "FAILED: pushed ${reference}, but \`docker image inspect\` on it failed, so there is no digest to pin." >&2
  echo "The image IS in the registry. Do not build it again; read its digest and write the pin by hand." >&2
  exit 1
fi
digest="$(awk -F@ -v repo="$repository" '$1 == repo { print $2 }' <<<"$digests")"
if ! [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "FAILED: pushed ${reference}, but Docker reports no single digest for it: '${digest}'" >&2
  exit 1
fi

echo
echo "Pushed. The pin for image.env:"
echo "SAVETHEDATE_IMAGE=${reference}@${digest}"
