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

for name in HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy IDF_TARGET; do
    if [[ -n ${!name:-} ]]; then
        docker_args+=(-e "$name=${!name}")
    fi
done

if [[ $# -eq 0 ]]; then
    set -- idf.py --help
fi

exec docker run "${docker_args[@]}" "$image" "$@"
