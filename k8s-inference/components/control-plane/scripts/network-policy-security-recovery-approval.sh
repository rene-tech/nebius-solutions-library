#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${script_dir}/network_policy_security_recovery_approval.py" "$@"
