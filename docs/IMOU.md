# iMou cameras (Open Platform), watch only, live view and memory-card playback

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
* **Recordings (memory card).** Playback tab: pick a day and press **Refresh** to read the list from the camera (nothing
  is read before that, nothing polls; the list is kept for 60 seconds). **Play** opens the player window: play from a
  clip, pause, click the timeline to jump. 1x only, no download. A jump or a resume asks the cloud for a new address
  (one request, at least 1.5 seconds apart). Pause stops the stream, so no flow is spent while paused. Playback and live
  view of one camera never run together: Play stops that camera's live view, and the tile asks for a click afterwards.
* Playback counts in the **same daily minutes and the same stream cap** as live view (the connection window says "live
  and playback minutes"). One playback run stops after 10 minutes: press Play to continue.
* Rename works locally in this app only. Nothing is written to the camera or to the iMou account.

## Cost and limits

The iMou free tier is small: 2 GB of video flow and 30,000 requests per month for the developer account. Live view and
playback use it. Read the real numbers under "My Resources" in the console before and after a one-minute test. Do not add funds
until you know what a minute costs.

When the vendor says the quota is used (FL1002, FL1004, FL1005, OP1013, OP1014, OP1040), Tapo Desktop stops **all** iMou calls
until the next UTC day and shows "free quota used". After a refused key (SN1001) nothing is sent for 2 minutes and
there is no silent retry; check the App ID and App secret. OP1039 ("too many requests this minute") stops calls until the next
minute only.

## Safety rules in the code

* The client can only build these requests: `accessToken`, `listDeviceDetailsByPage`,
  `createVideoDownloadStreamUrl` (only `businessType` `real` or `localRecord`, never cloud recordings), `bindDeviceLive`,
  `liveList`, `unbindLive`, and `v2/device.service.invoke`. The last one runs any device action, so it has a second
  allowlist: the only `serviceId` it may carry is `record.file.list`; anything else is refused before it is signed.
  Tests list the paths, the service ids and the businessTypes and fail on anything else (no bind, unbind, reboot,
  format, configuration, Wi-Fi, firmware, rename, PTZ or purchase call).
* Playback asks for one temporary RTSP address (`localRecord`) and never falls back to a cloud live address.
* Live view asks for a temporary RTSP address first. Only if that call is refused is a cloud HLS address created, and it
  is deleted when the stream stops.
* A live or playback address is a secret. It is never logged and never shown; error texts are cleaned of addresses.
* Every other action says "Not supported for iMou cameras yet".

## Owner checks on real hardware (not verified yet)

Read-only, a few minutes. Read "My Resources" before and after.

1. **Does the camera list its card?** Playback tab, Refresh. "this camera cannot list its recordings through the iMou
   cloud (DV2003)" means the camera does not offer the list; the older call is not built.
2. **Is the time right?** Play a recording from about five minutes ago for 30 seconds. The camera burns its own clock
   into the picture; it must match the time in the player. If it is off by whole hours, set `utc_offset_min` (minutes east
   of UTC, for example 420) in that camera's data. The status line says "the camera reports two clocks" when the vendor's
   two time fields differ.
3. **Pacing and length.** Does the picture move at normal speed? Does a playback run for the whole clip, and across the
   gap to the next clip? How many MB does one minute use?
4. Time zones: a camera in a zone with daylight saving needs its offset changed twice a year.

## Not in version 1

Download of recordings, 2x/4x/8x speed, cloud clips, snapshots, motion alarms, PTZ, rename on the camera, cameras that belong to someone
else's account, and two-way talk. The spec lane is `specs/imou_spec.json` (documentation evidence only).
