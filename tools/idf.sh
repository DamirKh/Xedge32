#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
image=${XEDGE_IDF_IMAGE:-xedge32-idf:6.1}
base_image=${XEDGE_IDF_BASE_IMAGE:-espressif/idf:v6.1}
ccache_dir=${XDG_CACHE_HOME:-"$HOME/.cache"}/xedge32/ccache

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required" >&2
    exit 1
fi

mkdir -p "$ccache_dir"

build_log=$(mktemp)
trap 'rm -f "$build_log"' EXIT
if ! docker build \
    --build-arg "IDF_IMAGE=$base_image" \
    -f "$repo_root/tools/Dockerfile" \
    -t "$image" \
    "$repo_root/tools" >"$build_log" 2>&1; then
    cat "$build_log" >&2
    exit 1
fi
rm -f "$build_log"
trap - EXIT

docker_args=(
    --rm
    --user "$(id -u):$(id -g)"
    -e HOME=/tmp
    -e USER="$(id -un)"
    -e LOGNAME="$(id -un)"
    -e CCACHE_DIR=/ccache
    -v "$repo_root:/project"
    -v "$ccache_dir:/ccache"
    -w /project
)

declare -A serial_groups=()
for dev in /dev/ttyUSB* /dev/ttyACM*; do
    if [[ -e "$dev" ]]; then
        docker_args+=(--device "$dev")
        serial_groups["$(stat -c %g "$dev")"]=1
    fi
done
for gid in "${!serial_groups[@]}"; do
    docker_args+=(--group-add "$gid")
done

for name in HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy IDF_TARGET XEDGE_WIFI_PASS; do
    if [[ -n ${!name:-} ]]; then
        docker_args+=(-e "$name=${!name}")
    fi
done

if [[ $# -eq 0 ]]; then
    set -- idf.py --help
fi

exec docker run "${docker_args[@]}" "$image" "$@"
