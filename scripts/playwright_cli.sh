#!/usr/bin/env bash
set -euo pipefail

if ! command -v npx >/dev/null 2>&1; then
  echo "Error: npx is required. Install Node.js/npm first." >&2
  exit 1
fi

# Windows npm invokes package binaries through cmd.exe. cmd.exe cannot use a WSL
# UNC path as its working directory, so launch from the Windows user directory.
npx_path="$(command -v npx)"
if [[ -n "${WSL_DISTRO_NAME:-}" && "$npx_path" == /mnt/* ]]; then
  windows_user_dir="${USERPROFILE:-}"
  if [[ -n "$windows_user_dir" && -d "$windows_user_dir" ]]; then
    cd "$windows_user_dir"
  fi
fi

has_session_flag="false"
for argument in "$@"; do
  case "$argument" in
    -s=*|--session|--session=*)
      has_session_flag="true"
      break
      ;;
  esac
done

command=(npx --yes --package @playwright/cli playwright-cli)
if [[ "$has_session_flag" != "true" && -n "${PLAYWRIGHT_CLI_SESSION:-}" ]]; then
  command+=(--session "$PLAYWRIGHT_CLI_SESSION")
fi
command+=("$@")

exec "${command[@]}"
