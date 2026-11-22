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
apply_files() {
  local patch="$here/$1" source
  while IFS= read -r source; do
    if git -C "$checkout" apply --check --reverse --include="$source" "$patch" 2>/dev/null; then
      continue
    fi
    git -C "$checkout" apply --check --include="$source" "$patch"
    git -C "$checkout" apply --include="$source" "$patch"
  done < <(sed -n 's|^+++ b/||p' "$patch")
}

apply_files hermes-projects.patch
apply_files hermes-project-delete.patch
apply_files hermes-telegram.patch
