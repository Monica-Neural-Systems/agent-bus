# Screenshot Bridge (Android)

Sends new screenshots from the phone's Screenshots folder to the Cloudflare
Worker (`/ingest`) over authenticated HTTPS. No Drive, no inbound ports.

## Privacy design
- ZERO storage/media permissions in the manifest.
- The user picks the Screenshots folder once via Storage Access Framework;
  Android enforces the folder boundary at OS level. The app cannot see
  anything outside the granted folder.
- Read-only: originals are never modified, moved, or deleted.
- Outbound HTTPS only, Bearer-token auth. Dedup by filename watermark.

## Build (reproducible)
Push to `main` (or run manually): GitHub Actions builds the release APK
(`assembleRelease`, JDK 17, Android SDK 34) and uploads it as the
`screenshot-bridge-apk` artifact, retained 30 days.

Local build (needs Android SDK + JDK 17):
```
cd android-screenshot-bridge
gradle assembleRelease
# APK: app/build/outputs/apk/release/app-release.apk
```

## Install (Pixel 9 Pro XL)
1. Download `screenshot-bridge-apk` from the Actions run.
2. Transfer to the phone; tap the APK (allow install-unknown-apps for the file manager).
3. Open the app: tap "Choose Screenshots folder" -> navigate to DCIM/Screenshots -> "Use this folder" -> Allow.
4. Paste the Worker URL and the ingest shared secret (must match the worker's INGEST_SECRET).
5. Tap "Save + Start bridge". WorkManager checks every 15 min.
