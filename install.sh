#!/usr/bin/env bash
# Symlink nakama into place. Re-run after moving the clone; `git pull` needs no re-run.
# Usage: ./install.sh [--uninstall]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

# Physical path of a link target: a symlink may record this clone under any spelling of its path.
resolved() {
  local path="$1" base="${2:-.}"
  case "$path" in /*) ;; *) path="$base/$path" ;; esac
  echo "$(cd "$(dirname "$path")" && pwd -P)/$(basename "$path")"
}

LINKS=(
  "$HOME/.local/bin/nakama|$REPO/bin/nakama"
  "$HOME/.agents/skills/nakama|$REPO/skills/nakama"
  "$HOME/.agents/skills/nakama-orchestrate|$REPO/skills/nakama-orchestrate"
  "$HOME/.claude/skills/nakama|$REPO/skills/nakama"
  "$HOME/.claude/skills/nakama-orchestrate|$REPO/skills/nakama-orchestrate"
)
uninstall=false
[ "${1:-}" = "--uninstall" ] && uninstall=true
status=0

for pair in "${LINKS[@]}"; do
  link="${pair%%|*}"
  target="${pair#*|}"
  ours=false
  [ -L "$link" ] && [ "$(resolved "$(readlink "$link")" "$(dirname "$link")")" = "$(resolved "$target")" ] && ours=true
  if $uninstall; then
    if $ours; then rm "$link" && echo "removed $link"; fi
    continue
  fi
  if $ours; then
    echo "ok      $link"
  elif [ -e "$link" ] || [ -L "$link" ]; then
    echo "SKIP    $link already exists and is not nakama's link; move it and re-run" >&2
    status=1
  else
    mkdir -p "$(dirname "$link")"
    ln -s "$target" "$link"
    echo "linked  $link"
  fi
done

if ! $uninstall; then
  "$REPO/bin/nakama" doctor
fi
exit "$status"
