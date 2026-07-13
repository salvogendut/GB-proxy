#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
TOPDIR=${RPM_TOPDIR:-"${HOME}/rpmbuild"}
REF=${1:-HEAD}

for command in git rpmbuild rpmspec tar; do
	if ! command -v "${command}" >/dev/null 2>&1; then
		echo "${command} is required." >&2
		exit 1
	fi
done

VERSION=$(rpmspec -q --qf '%{version}\n' "${PROJECT_DIR}/gb-proxy.spec" | head -n 1)
SOURCE_DIR="${TOPDIR}/SOURCES"
SOURCE_ARCHIVE="${SOURCE_DIR}/GB-proxy-${VERSION}.tar.gz"
mkdir -p "${SOURCE_DIR}"

git -C "${PROJECT_DIR}" archive \
	--format=tar.gz \
	--prefix="GB-proxy-${VERSION}/" \
	--output="${SOURCE_ARCHIVE}" \
	"${REF}"
tar --extract \
	--gzip \
	--file="${SOURCE_ARCHIVE}" \
	--directory="${SOURCE_DIR}" \
	--strip-components=2 \
	"GB-proxy-${VERSION}/packaging/gb-proxy.sysusers"
chmod 0644 "${SOURCE_DIR}/gb-proxy.sysusers"

exec rpmbuild -ba \
	--define "_topdir ${TOPDIR}" \
	"${PROJECT_DIR}/gb-proxy.spec"
