#!/usr/bin/env bash
# Build a standalone ostack9s binary with PyInstaller.
# Usage: packaging/build-binary.sh [DIST_DIR]   (default: dist)
set -euo pipefail

cd "$(dirname "$0")/.."
dist="${1:-dist}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# openstacksdk and keystoneauth load auth plugins, cache backends and service
# data at runtime (entry points, dynamic imports): collect them explicitly.
uv run --no-active --with pyinstaller pyinstaller \
  --onefile --name ostack9s --noconfirm --log-level WARN \
  --distpath "$dist" --workpath "$work/build" --specpath "$work" \
  --collect-data ostack9s --collect-data textual \
  --collect-data openstack --collect-data os_service_types \
  --collect-submodules openstack --collect-submodules keystoneauth1 \
  --collect-submodules dogpile --collect-submodules stevedore \
  --copy-metadata ostack9s --copy-metadata openstacksdk \
  --copy-metadata keystoneauth1 --copy-metadata os-service-types \
  --copy-metadata stevedore --copy-metadata dogpile.cache \
  --exclude-module keystoneauth1.fixture \
  packaging/entry.py

"$dist/ostack9s" --version
