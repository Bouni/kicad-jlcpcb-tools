#!/bin/sh
set -eu

# Example post-generation hook:
# - stages changed files from the project directory
# - creates a commit like:
#     Generation checkpoint: <project> <YYYY>-<MM>-<DD>-<version>
# - creates a lightweight tag for the same checkpoint
# - pushes commit + tag to origin

if [ "${JLCPCB_HOOK_STAGE:-}" != "post" ]; then
  echo "This example is intended for post-generate hooks only (current stage: ${JLCPCB_HOOK_STAGE:-unset})."
  exit 0
fi

if [ -z "${JLCPCB_PROJECT_DIR:-}" ]; then
  echo "JLCPCB_PROJECT_DIR is not set."
  exit 1
fi

cd "$JLCPCB_PROJECT_DIR"

project_name=$(basename "$JLCPCB_PROJECT_DIR")
date_stamp=$(date +%Y-%m-%d)
version="${JLCPCB_GENERATION_COUNT:-0}"
checkpoint_id="${date_stamp}-${version}"
commit_message="Generation checkpoint: ${project_name} ${checkpoint_id}"

# Tag names cannot contain spaces or many special characters.
safe_project_name=$(printf "%s" "$project_name" | tr " /" "--" | tr -cd "[:alnum:]._-")
tag_name="gen-${safe_project_name}-${checkpoint_id}"

echo "Staging changes from: $JLCPCB_PROJECT_DIR"
git add -A .

if git diff --cached --quiet; then
  echo "No staged changes after 'git add -A .'; skipping commit/tag/push."
  exit 0
fi

echo "Committing: $commit_message"
git commit -m "$commit_message"

echo "Tagging: $tag_name"
git tag "$tag_name"

echo "Pushing commit to origin"
git push origin HEAD

echo "Pushing tag to origin"
git push origin "$tag_name"

echo "Done."
