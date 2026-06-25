#!/bin/bash
# Arctic (Homey CLI): Build + lokales Install auf 192.168.188.62
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Python-Dependencies (Docker) ==="
homey app dependencies install

echo "=== Lokal installieren (Homey 192.168.188.62) ==="
homey app install

echo "✅ Lokales Homey-Update fertig."