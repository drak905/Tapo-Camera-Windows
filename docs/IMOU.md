# iMou cameras (Open Platform), watch only, version 1

Unofficial. Tapo Desktop can show cameras from an iMou (Imou Life) account through the **official iMou Open Platform
API**. It uses only public, documented calls. It does not use the Imou Life app, its protocol or its keys.

## What you need

1. A free developer account on the [iMou Open Platform](https://open.imoulife.com) (international portal), made with
   **the same iMou account that holds your cameras**. The cameras must show up in the portal's device list. Nothing is
   bound, unbound or changed by Tapo Desktop; if a camera is missing there, it is not visible to this client.
2. In the console, create an app and copy its **App ID** and **App secret**. Note the **data center** shown under
   "My information" (Singapore `sg`, Frankfurt `fk` or Oregon `or`).
3. In Tapo Desktop: account button, **iMou connection...**. Enter App ID, App secret, pick the data center, click
   **Test**. The test makes one token call and one device-list call. When it works, the cameras appear in the list.

The App ID and App secret are stored only in the Windows-protected vault (`vault.json`, readable by your Windows user).
They are never written to `config.json`, to the log, or into a web address.

## Using it

* An iMou tile shows "iMou - click to start". **Click the tile** to start live view. Nothing starts by itself and
  nothing is polled. Live view starts with a delay (up to about 30 seconds).
* Grid tiles use the small (SD) stream. Full screen uses the large (HD) stream.
* A live view stops by itself after **10 minutes** and asks for a click again. Each camera has a daily budget (default
  **20 minutes**, a local estimate); at most **2** iMou streams run at once. Both numbers are in the connection window.
* **Refresh iMou cameras** (account menu) reads the camera list and the online state again. **Disconnect iMou**
  removes the iMou cameras and the saved key.
* Rename works locally in this app only. Nothing is written to the camera or to the iMou account.

## Cost and limits

The iMou free tier is small: 2 GB of video flow and 30,000 requests per month for the developer account. Live view uses
it. Read the real numbers under "My Resources" in the console before and after a one-minute test. Do not add funds
until you know what a minute costs.

When the vendor says the quota is used (FL1002, FL1004, FL1005, OP1013, OP1014), Tapo Desktop stops **all** iMou calls
until the next UTC day and shows "free quota used". After a refused key (SN1001) nothing is sent for 2 minutes and
there is no silent retry; check the App ID and App secret.

## Safety rules in the code

* The client can only build these requests: `accessToken`, `listDeviceDetailsByPage`,
  `createVideoDownloadStreamUrl`, `bindDeviceLive`, `liveList`, `unbindLive`. A test lists them and fails on anything
  else (no bind, unbind, reboot, configuration, Wi-Fi, firmware, rename, PTZ or purchase call).
* Live view asks for a temporary RTSP address first. Only if that call is refused is a cloud HLS address created, and it
  is deleted when the stream stops.
* A live address is a secret. It is never logged and never shown; error texts are cleaned of addresses.
* Every other action says "Not supported for iMou cameras yet".

## Not in version 1

SD-card playback, cloud clips, snapshots, motion alarms, PTZ, rename on the camera, cameras that belong to someone
else's account, and two-way talk. The spec lane is `specs/imou_spec.json` (documentation evidence only).
