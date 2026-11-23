#!/usr/bin/env bash
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
checkout="${HERMES_HOME:-$HOME/.hermes}/hermes-agent"
package="$checkout/hermes_cli/workspace_projects"

# Native project management is shared by the webapp, CLI and chat tool.
if [[ ! -e "$package" && ! -L "$package" ]]; then
  ln -s "$here/project-cli" "$package"
elif [[ "$(realpath "$package")" != "$here/project-cli" ]]; then
  echo "기존 Hermes 모듈과 경로가 겹칩니다 · $package" >&2
  exit 1
fi
# Patches can touch the same hunk on successive releases. Reconstruct the base
# in a scratch tree first so clean installs and partially upgraded hosts converge.
patches=(hermes-projects hermes-project-delete hermes-telegram hermes-block-request hermes-attempt-logs hermes-readonly-projects hermes-observer-workflows)
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
files() { sed -n 's|^+++ b/||p' "$here/$1.patch"; }
mapfile -t paths < <(for patch in "${patches[@]}"; do files "$patch"; done | sort -u)
for source in "${paths[@]}"; do
  mkdir -p "$scratch/$(dirname "$source")"
  cp -p "$checkout/$source" "$scratch/$source"
done
for (( i=${#patches[@]}-1; i>=0; i-- )); do
  patch=${patches[i]}
  while IFS= read -r source; do
    args=(--reverse --include="$source" "$here/$patch.patch")
    if git -C "$scratch" apply --check "${args[@]}" 2>/dev/null; then
      git -C "$scratch" apply "${args[@]}"
    fi
  done < <(files "$patch")
done
for patch in "${patches[@]}"; do
  git -C "$scratch" apply "$here/$patch.patch"
done
for source in "${paths[@]}"; do
  if ! cmp -s "$scratch/$source" "$checkout/$source"; then
    cp "$scratch/$source" "$checkout/$source"
  fi
done
