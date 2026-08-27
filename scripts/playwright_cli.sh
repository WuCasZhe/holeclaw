#!/usr/bin/env bash
set -euo pipefail

# Windows npm invokes package binaries through cmd.exe. cmd.exe cannot use a WSL
# UNC path as its working directory, so launch from the Windows user directory.
npx_path="${PLAYWRIGHT_CLI_NPX:-}"
if [[ -z "$npx_path" && -n "${WSL_DISTRO_NAME:-}" ]] \
  && ! command -v google-chrome >/dev/null 2>&1 \
  && [[ -x "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe" ]] \
  && [[ -x "/mnt/c/Program Files/nodejs/npx" ]]; then
  npx_path="/mnt/c/Program Files/nodejs/npx"
fi
if [[ -z "$npx_path" ]]; then
  npx_path="$(command -v npx || true)"
fi
if [[ -z "$npx_path" ]]; then
  echo "Error: npx is required. Install Node.js/npm first." >&2
  exit 1
fi

if [[ -n "${WSL_DISTRO_NAME:-}" && "$npx_path" == /mnt/* ]]; then
  windows_user_dir="${USERPROFILE:-}"
  if [[ ! -d "$windows_user_dir" ]] && command -v cmd.exe >/dev/null 2>&1; then
    windows_user_dir="$(cmd.exe /d /c 'echo %USERPROFILE%' 2>/dev/null | tr -d '\r')"
    windows_user_dir="$(wslpath -u "$windows_user_dir" 2>/dev/null || true)"
  fi
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

command=("$npx_path" --yes --package @playwright/cli playwright-cli)
if [[ "$has_session_flag" != "true" && -n "${PLAYWRIGHT_CLI_SESSION:-}" ]]; then
  command+=(--session "$PLAYWRIGHT_CLI_SESSION")
fi
command+=("$@")

exec "${command[@]}"
