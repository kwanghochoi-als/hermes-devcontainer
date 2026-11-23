#!/usr/bin/env bash
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export HERMES_TEST_INSTALL=${1:-"${HERMES_HOME:-$HOME/.hermes}/hermes-agent"}
HERMES_TEST_INSTALL=$(realpath "$HERMES_TEST_INSTALL")
revision=${2:-$(<"$here/hermes-revision")}
export HERMES_HOME
HERMES_HOME=$(dirname "$HERMES_TEST_INSTALL")
export PATH="$HERMES_TEST_INSTALL/.hermes/bin:$PATH"
test -x "$HERMES_TEST_INSTALL/.hermes/bin/hermes"
docker inspect --type container "${HERMES_CLI_CONTROLLER:-agent-cli}" >/dev/null
bash "$here/setup-projects.sh" --checkout "$HERMES_TEST_INSTALL" --revision "$revision" --verify
hermes plugins validate "$here/project-cli"
hermes webapp --build-only
python3 -B -m unittest discover -s "$here/tests" -v
python3 -B -m unittest discover -s "$here/observer/.observer" -p test_gate.py -v
