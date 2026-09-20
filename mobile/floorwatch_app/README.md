# Floorwatch Employee App

Phone+OTP login, task list, and task actions (start/done/request more
time/ask a supervisor/hand off) — talks to the employee mobile-app API
added to `services/floorwatch-rules-engine/app/main.py` (see
`dazzling-hopping-comet.md` for the full backend design).

## Running it

The API base URL is never hardcoded — pass it at build/run time:

```bash
flutter run --dart-define=FLOORWATCH_API_BASE_URL=https://your-deployment.up.railway.app
```

Without it, every API call fails with a clear "not configured" error
rather than silently pointing at the wrong server.

## Push notifications — configured in Railway

Nothing Firebase-related is committed or baked into the build. Set these
Railway variables on the rules-engine service, then redeploy:

| Variable | Value |
|---|---|
| `FIREBASE_API_KEY` (or `FLOORWATCH_FIREBASE_API_KEY`) | the Firebase API key |
| `FLOORWATCH_FCM_CREDENTIALS_JSON` | the whole service-account key JSON (Firebase → Project settings → Service accounts → Generate new private key) — lets the backend *send* pushes |
| `FIREBASE_APP_ID`, `FIREBASE_PROJECT_ID`, `FIREBASE_SENDER_ID` | optional overrides; default to this project's values |

After login the app fetches its Firebase settings from
`GET /api/employee/app-config` (login-required) and caches them on the device.
`android/.../FloorwatchApplication.kt` starts Firebase from that cache on every
launch — including when Android starts the app only to show a push while it's
closed, where no Dart code runs (which is why this can't be done from Dart
alone). So the very first login registers the device; pushes work from then on.
`/healthz` reports `app_push_config_ready` (API key present) and `fcm_ready`
(service-account key valid). The Android app in Firebase must have package name
`com.floorwatch.floorwatch_app`, and everything must be the **same** Firebase
project.

iOS additionally needs an APNs Auth Key (Apple Developer account) uploaded
under Firebase Project settings → Cloud Messaging, plus an iOS app's
`GoogleService-Info.plist` values. `UIBackgroundModes: remote-notification` is
already set in `Info.plist`; in Xcode also enable the **Push Notifications**
and **Background Modes → Remote notifications** capabilities on the Runner
target.

## Live updates

`lib/services/live_updates.dart` keeps a WebSocket to the backend's
`/ws/app`. The socket carries no data — only a "something changed, refetch"
hint, scoped server-side (a plain employee hears only about their own
tasks; supervisors, admins and admin-login accounts hear about everything).
Screens refetch over normal authenticated REST calls. If the socket drops
it reconnects with backoff, and the screens also refresh on returning to the
app and on a slow poll.

## Building iOS — needs a Mac

This dev environment doesn't have Xcode. Once on a Mac with this repo:

```bash
flutter build ios --dart-define=FLOORWATCH_API_BASE_URL=https://your-deployment.up.railway.app
```

or open `ios/Runner.xcworkspace` in Xcode directly for signing/
TestFlight submission (needs an active Apple Developer Program
membership).

## Building Android

```bash
flutter build apk --dart-define=FLOORWATCH_API_BASE_URL=https://your-deployment.up.railway.app
```

The resulting APK can be sideloaded directly for testing — no Play
Console account needed until you want Play Store distribution.
