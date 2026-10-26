#!/bin/bash

echo "Building Xedge32 resource file main/BAS/examples/xedge/obj/XedgeZip.c"

directories=("main/BAS" "main/BAS-Resources" "main/lua-protobuf")
for dir in "${directories[@]}"; do
    if [ ! -d "$dir" ]; then
        echo "Directory '$dir' does not exist."
        echo "Please make sure you cloned the repository recursively."
        exit 1
    fi
done

mkdir -p main/BAS/examples/xedge/obj || exit 1

export BIN2C=main/BAS-Resources/tools/linux/bin2c
#Resource build directory
RB=main/BAS-Resources/build
pushd $RB || exit 1
chmod +x Xedge.sh
echo "Running $PWD/Xedge.sh"
export NO_BIN2C=1
./Xedge.sh || exit 1
popd

echo "Patching Xedge compile-time HTTP date parsing"
python3 - "$RB/XedgeBuild/.lua/xedge.lua" <<'PY' || exit 1
import sys
path = sys.argv[1]
old = '   local tm=ba.parsedate("Mon, "..date:gsub("^(%w+)%s*(%w+)","%2 %1"))'
new = '''   local tm=assert(ba.parsedate("Mon, "..date:gsub("^(%w+)%s+(%d+)",
      function(month,day) return sfmt("%02d %s",tonumber(day),month) end).." GMT"))'''
with open(path, encoding="utf-8") as fp:
    text = fp.read()
if old in text:
    text = text.replace(old, new, 1)
elif new not in text:
    raise SystemExit("unexpected xedge.lua compile-time date parser")
with open(path, "w", encoding="utf-8") as fp:
    fp.write(text)
PY
(cd "$RB/XedgeBuild" && zip -q -d ../Xedge.zip .lua/xedge.lua && zip -q ../Xedge.zip .lua/xedge.lua) || exit 1

echo "Adding ESP32 modules"
if [ "$NO_FWUPDATE" == "1" ]; then
    zip -D -r -9 $RB/Xedge.zip .lua -x ".lua/XedgePlugins/fw*" || exit 1
else
    zip -d main/BAS-Resources/build/Xedge.zip ".lua/XedgePlugins/appupdate.lua"
    zip -D -r -9 $RB/Xedge.zip  .lua || exit 1
fi

$BIN2C -z getLspZipReader $RB/Xedge.zip  main/BAS/examples/xedge/obj/XedgeZip.c || exit 1
echo "Created: main/BAS/examples/xedge/obj/XedgeZip.c"



