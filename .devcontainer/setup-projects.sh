#!/usr/bin/env bash
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
checkout="${HERMES_HOME:-$HOME/.hermes}/hermes-agent"
package="$checkout/hermes_cli/workspace_projects"
patch="$here/hermes-projects.patch"

# The webapp, built-in CLI and chat tool share projects_db.create_project.
# Hermes currently has no project-create plugin hook.
if [[ ! -e "$package" && ! -L "$package" ]]; then
  ln -s "$here/project-cli" "$package"
elif [[ "$(realpath "$package")" != "$here/project-cli" ]]; then
  echo "기존 Hermes 모듈과 경로가 겹칩니다 · $package" >&2
  exit 1
fi
for source in hermes_cli/projects_db.py tools/project_tools.py apps/desktop/src/app/chat/sidebar/project-dialog.tsx; do
  if git -C "$checkout" apply --check --reverse --include="$source" "$patch" 2>/dev/null; then
    continue
  fi
  git -C "$checkout" apply --check --include="$source" "$patch"
  git -C "$checkout" apply --include="$source" "$patch"
done

for source in hermes_cli/kanban_db.py tools/clarify_tool.py; do
  if git -C "$checkout" apply --check --reverse --include="$source" "$here/hermes-telegram.patch" 2>/dev/null; then
    continue
  fi
  git -C "$checkout" apply --check --include="$source" "$here/hermes-telegram.patch"
  git -C "$checkout" apply --include="$source" "$here/hermes-telegram.patch"
done
