#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
prefix="$HOME/.local/share/hermes-qmd"
package="$prefix/lib/node_modules/@tobilu/qmd"
qmd_version=2.9.0
qmd_dir="${HERMES_HOME:-$HOME/.hermes}/qmd"
launcher="$HOME/.local/bin/qmd"

if [[ ! -x "$prefix/bin/qmd" || "$("$prefix/bin/qmd" --version)" != "qmd $qmd_version "* ]]; then
  npm install --global --prefix "$prefix" --no-audit --no-fund "@tobilu/qmd@$qmd_version"
fi
install -d -m 700 "$qmd_dir"
install -Dm755 "$script_dir/qmd" "$launcher"

# Use QMD's own locked YAML update so collections added later survive rebuilds.
QMD_CONFIG_DIR="$qmd_dir" node --input-type=module - "$package" "$script_dir" <<'JS'
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
const [pkg, guideDirectory] = process.argv.slice(2);
const { configExists, loadConfig, updateConfig } = await import(pathToFileURL(join(pkg, 'dist/collections.js')));
const embed = 'http://host.docker.internal:11434/v1#qwen3-embedding:8b';
const initialize = !configExists();
if (initialize || loadConfig().models?.embed !== embed) {
  updateConfig(current => {
    current.models ??= {};
    if (current.models.embed !== embed) {
      delete current.models.embedOptions;
      current.models.embed = embed;
    }
    if (initialize) {
      current.collections['hermes-workspace'] ??= { path: guideDirectory, pattern: 'README.md' };
    }
  });
}
JS

# Populate a fresh index before exposing it to the MCP client.
if [[ ! -f "$qmd_dir/index.sqlite" ]]; then
  "$launcher" update
  "$launcher" embed || echo 'Ollama 연결 후 qmd embed 명령으로 색인을 완료하세요.' >&2
fi

hermes config set mcp_servers.qmd.command "$launcher"
hermes config set mcp_servers.qmd.args '["mcp"]'
