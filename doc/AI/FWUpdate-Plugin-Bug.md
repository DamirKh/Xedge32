# Xedge IDE three-dot menu crash (fwupdate plugin)

## Status

Reproducible on the custom Xedge32 build used by the `myPLC` thermostat.
Fixed in the working tree by patching the legacy plugin, clearing the stale
resource archive before regeneration, and rebuilding the S3 app. Browser
verification on the target AP is still pending.

- Target: ESP32-S3, 8 MB partition layout, USB Serial/JTAG console.
- Firmware: custom Xedge32, `6b949ca-dirty` (`toolchain-setup` branch).
- Board: Xedge32 running in AP mode, IDE available at `http://192.168.190.0/rtl/`.

## Symptom

Clicking the three-dot menu (`#IdeCfg`) does nothing. The menu never opens.

Chrome DevTools reports:

```text
TypeError: m.children is not iterable
    at <anonymous>:2:15
    at http://192.168.190.0/rtl/assets/xedge.js:1414:29
    at ideCfg (http://192.168.190.0/rtl/assets/xedge.js:1414:13)
```

The failing plugin is loaded as `fwupdate.js` and contains:

```js
ideCfgCB.push(m => {
  let b = [...m.children].find(e => e.textContent === "App Upload");
  if (b) b.textContent = "Firmware Update & App Upload";
  else m.append(b = el("li", { text: "Firmware Update & App Upload" }));
  ...
});
```

## Root cause

There is an API mismatch between `xedge.js` and the legacy root plugin. It is
not a stale generated-resource problem.

- `xedge.js` calls plugin callbacks with a jQuery object:

  ```js
  ideCfgCB.forEach((cb) => cb(mlist, nodisk));
  ```

- The embedded `fwupdate.js` expects `m` to be a DOM element and does
  `[...m.children]`. On a jQuery object, `m.children` is a function, not an
  iterable, so the callback throws.
- The same callback also uses `m.append(...)`, which is a jQuery method. The
  plugin is internally inconsistent: it mixes DOM and jQuery access.

The checked-out source at:

```text
main/BAS-Resources/src/xedge/.lua/XedgePlugins/appupdate.js
```

uses the correct jQuery API:

```js
ideCfgCB.push(function (mlist) {
  mlist.append($('<li>').text("App Upload").on("click", () => { ... }));
});
```

The broken plugin is the legacy file in the repository root:

```text
.lua/XedgePlugins/fwupdate.js
```

It was missed by the initial investigation because it is outside
`main/BAS-Resources`. `BuildESP32ResourceFile.sh` deliberately adds the root
`.lua` directory to the embedded Xedge ZIP as the ESP32 module layer, so this
legacy plugin is shipped alongside the newer `appupdate.js`.

`Xedge.sh` removes a lowercase `xedge.zip`, but this project uses `Xedge.zip`.
As a result, old ZIP entries could also survive a resource rebuild. The build
script now removes the upper-case archive explicitly before rebuilding, which
prevents another class of stale-resource drift.

## Fix

### Fix the legacy plugin and rebuild resources

The root plugin must use the same jQuery object that `xedge.js` passes to every
`ideCfgCB` callback:

```js
ideCfgCB.push(function (mlist) {
  let b = mlist.children("li").filter(function () {
    return this.textContent === "App Upload";
  }).first();
  if (b.length) b.text("Firmware Update & App Upload");
  else b = $('<li>').text("Firmware Update & App Upload").appendTo(mlist);
  b.on("click", () => sendCmd("getfwver", r => { ... }));
});
```

Do not change `ideCfgCB.forEach((cb) => cb(mlist, nodisk));` to pass a DOM
element. `appupdate.js` and the other plugins expect the jQuery object.

Then rebuild the embedded resources and the S3 app:

```bash
cd /home/damir/p/mxe32
XEDGE_REBUILD_RESOURCES=1 ./tools/build.sh esp32s3
./tools/idf.sh idf.py -B build/esp32s3 \
  -D SDKCONFIG=build/esp32s3/sdkconfig \
  -p /dev/ttyACM0 -b 230400 app-flash
```

Check the generated archive:

```bash
unzip -p main/BAS-Resources/build/Xedge.zip \
  .lua/XedgePlugins/fwupdate.js | sed -n '1,20p'
unzip -p main/BAS-Resources/build/Xedge.zip \
  .lua/XedgePlugins/appupdate.js | grep -n 'ideCfgCB'
```

Expected result: `fwupdate.js` uses `mlist.children("li")`, `b.text(...)`, and
`b.on(...)`; no `[...m.children]` remains.

A local browser harness that loads jQuery, `appupdate.js`, and the patched
`fwupdate.js` produced one menu item:

```text
ERR=|COUNT=1|TEXT=Firmware Update & App Upload
```

## Verification

1. Flash the rebuilt app image.
2. Open `http://<board>/rtl/` in Chrome with DevTools open.
3. Click the three-dot menu.
4. Confirm there is no `TypeError: m.children is not iterable`.
5. Confirm the menu opens and **Firmware Update & App Upload** is present.
6. Confirm the browser loads the patched plugin without a `m.children` error.
7. Upload a test ZIP in developer mode and confirm the app installs/starts.

## Notes for the maintainer

- The root problem is the legacy root plugin using DOM APIs with a jQuery
  callback argument, not the browser and not Wi-Fi.
- Keep the Xedge IDE resources (`xedge.js` and plugins) rebuilt together.
- Review `.lua/XedgePlugins/fwupdate.js` whenever the `ideCfgCB` callback
  contract changes.
- If the build process is changed, make resource regeneration mandatory whenever
  `main/BAS-Resources` changes.
- Do not commit AP names, passwords, or MAC addresses in the fix or test notes.
