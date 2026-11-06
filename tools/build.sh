#!/usr/bin/env bash
set -euo pipefail

target=${1:-esp32}
case "$target" in
    esp32|esp32p4)
        idf_target="$target"
        profile=""
        build_dir="build/$target"
        ;;
    esp32s3)
        idf_target="esp32s3"
        profile=""
        build_dir="build/esp32s3"
        ;;
    esp32s3-16mb)
        idf_target="esp32s3"
        profile="16mb"
        build_dir="build/esp32s3-16mb"
        ;;
    *)
        echo "usage: $0 [esp32|esp32s3|esp32s3-16mb|esp32p4]" >&2
        exit 1
        ;;
esac

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
idf="$repo_root/tools/idf.sh"
config_file="$build_dir/sdkconfig"
export IDF_TARGET="$idf_target"
if [[ -n "$profile" ]]; then
    export XEDGE_S3_PROFILE="$profile"
else
    unset XEDGE_S3_PROFILE
fi

cd "$repo_root"
mkdir -p "$build_dir"

if [[ ! -f main/BAS/examples/xedge/obj/XedgeZip.c || ${XEDGE_REBUILD_RESOURCES:-0} == 1 ]]; then
    "$idf" env USE_OPCUA=0 XedgeCaStore=large \
        bash -c 'printf "n\n" | bash ./BuildESP32ResourceFile.sh'
fi

"$idf" idf.py -B "$build_dir" -D "SDKCONFIG=$config_file" build size
