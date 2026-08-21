#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
REF=${1:-HEAD}
OUTPUT_DIR=${DEB_OUTPUT_DIR:-"${PROJECT_DIR}/dist/debian"}

for command in dpkg-buildpackage dpkg-parsechangelog git python3 tar; do
	if ! command -v "${command}" >/dev/null 2>&1; then
		echo "${command} is required." >&2
		exit 1
	fi
done

VERSION=$(python3 -c \
	"import configparser; c=configparser.ConfigParser(); c.read('${PROJECT_DIR}/setup.cfg'); print(c['metadata']['version'])")
BUILD_ROOT=$(mktemp -d)
ARCHIVE="${BUILD_ROOT}/gb-proxy-${VERSION}.tar"
SOURCE_DIR="${BUILD_ROOT}/gb-proxy-${VERSION}"
trap 'rm -rf "${BUILD_ROOT}"' EXIT

git -C "${PROJECT_DIR}" archive \
	--format=tar \
	--prefix="gb-proxy-${VERSION}/" \
	--output="${ARCHIVE}" \
	"${REF}"
tar --extract --file="${ARCHIVE}" --directory="${BUILD_ROOT}"

(
	cd "${SOURCE_DIR}"
	DEBIAN_VERSION=$(dpkg-parsechangelog --show-field Version)
	if [[ "${DEBIAN_VERSION}" != "${VERSION}-1" ]]; then
		echo "Debian version ${DEBIAN_VERSION} does not match project version ${VERSION}." >&2
		exit 1
	fi
	dpkg-buildpackage --build=binary --no-sign
)

mkdir -p "${OUTPUT_DIR}"
shopt -s nullglob
packages=("${BUILD_ROOT}"/gb-proxy_"${VERSION}"-*_all.deb)
if (( ${#packages[@]} != 1 )); then
	echo "Expected exactly one architecture-independent Debian package." >&2
	exit 1
fi
cp -v "${packages[0]}" "${OUTPUT_DIR}/"
