#!/usr/bin/env bash
set -euo pipefail

target=${1:-esp32}
case "$target" in
    esp32|esp32s3|esp32p4) ;;
    *)
        echo "usage: $0 [esp32|esp32s3|esp32p4]" >&2
        exit 1
        ;;
esac

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
idf="$repo_root/tools/idf.sh"
build_dir="build/$target"
config_file="$build_dir/sdkconfig"
export IDF_TARGET="$target"

cd "$repo_root"
mkdir -p "$build_dir"

if [[ ! -f main/BAS/examples/xedge/obj/XedgeZip.c || ${XEDGE_REBUILD_RESOURCES:-0} == 1 ]]; then
    "$idf" env USE_OPCUA=0 XedgeCaStore=large \
        bash -c 'printf "n\n" | bash ./BuildESP32ResourceFile.sh'
fi

"$idf" idf.py -B "$build_dir" -D "SDKCONFIG=$config_file" build size
