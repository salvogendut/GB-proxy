#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
VENV_DIR="${PROJECT_DIR}/venv"

if ! command -v python3 >/dev/null 2>&1; then
	echo "python3 is required." >&2
	exit 1
fi

if [ ! -x "${VENV_DIR}/bin/python" ]; then
	if [ -e "${VENV_DIR}" ]; then
		echo "${VENV_DIR} exists but is not a usable virtual environment." >&2
		echo "Move or remove it, then run this script again." >&2
		exit 1
	fi
	echo "Creating GB-proxy virtual environment..."
	python3 -m venv "${VENV_DIR}"
fi

if [ ! -x "${VENV_DIR}/bin/gb-proxy" ]; then
	echo "Installing GB-proxy and its core dependencies..."
	"${VENV_DIR}/bin/python" -m pip install --editable "${PROJECT_DIR}"
fi

exec "${VENV_DIR}/bin/gb-proxy" "$@"
