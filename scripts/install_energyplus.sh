#!/usr/bin/env bash
# Download and unpack EnergyPlus for Linux x86_64 (used by CI, Cloud Shell and Docker).
#
#   bash scripts/install_energyplus.sh [install_dir]     # default ~/energyplus
#
# Then:  export ENERGYPLUS_DIR=<install_dir> PATH=<install_dir>:$PATH
# Set GITHUB_TOKEN or GH_TOKEN to avoid GitHub's anonymous API rate limit.

set -euo pipefail

TAG="${ENERGYPLUS_TAG:-v25.1.0}"
DEST="${1:-$HOME/energyplus}"

if [[ -x "$DEST/energyplus" ]]; then
  echo "EnergyPlus already installed in $DEST"
  "$DEST/energyplus" --version
  exit 0
fi

# Prefer a build for this Ubuntu release, else fall back to the 22.04 build.
ubuntu="22.04"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" && -n "${VERSION_ID:-}" ]] && ubuntu="$VERSION_ID"
fi

token="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
auth=()
[[ -n "$token" ]] && auth=(-H "Authorization: Bearer $token")

echo "Looking up EnergyPlus $TAG release assets..."
assets="$(curl -fsSL "${auth[@]}" "https://api.github.com/repos/NREL/EnergyPlus/releases/tags/$TAG" \
  | python3 -c 'import json,sys; [print(a["browser_download_url"]) for a in json.load(sys.stdin)["assets"]]')"

url="$(grep -E "Linux-Ubuntu${ubuntu}-x86_64\.tar\.gz$" <<<"$assets" | head -1 || true)"
if [[ -z "$url" ]]; then
  url="$(grep -E 'Linux-Ubuntu22\.04-x86_64\.tar\.gz$' <<<"$assets" | head -1 || true)"
fi
[[ -n "$url" ]] || { echo "No Linux x86_64 tarball found for $TAG"; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
echo "Downloading $(basename "$url")..."
curl -fL --retry 3 -o "$tmp/eplus.tar.gz" "$url"
mkdir -p "$DEST"
tar -xzf "$tmp/eplus.tar.gz" -C "$DEST" --strip-components=1

"$DEST/energyplus" --version
echo
echo "Installed to $DEST. Add to your shell:"
echo "  export ENERGYPLUS_DIR=$DEST PATH=$DEST:\$PATH"
