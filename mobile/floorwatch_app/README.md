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

## Push notifications — requires a real Firebase project

Firebase is set up by the build (not from Dart at runtime): when a push
arrives for a closed app Android starts the process without running any Dart,
so a default Firebase app has to exist natively already or the notification
can't be shown. The API key is kept out of git:

1. Create a Firebase project with an Android app, package name
   `com.floorwatch.floorwatch_app`.
2. The committed template `android/app/firebase-config.json` is a
   `google-services.json` with its key left as `{{FIREBASE_API_KEY}}`. At build
   time Gradle copies it to the git-ignored `google-services.json` with the
   real key filled in. Supply the key (the `current_key` from the
   `google-services.json` you download from Firebase) to the build with either:
   - the environment variable `FIREBASE_API_KEY`, or
   - a line `FIREBASE_API_KEY=AIza...` in `android/firebase.properties`
     (git-ignored; not `local.properties`, which Flutter rewrites each build).
   Also worth restricting that key to the app's package name in Google Cloud
   console. (Railway can't supply it: the APK is built on your machine — but if
   you ever build in CI, set `FIREBASE_API_KEY` there as a secret.)
3. **Backend** (Railway): Firebase console → Project settings → Service
   accounts → Generate new private key. Paste the entire JSON as the variable
   `FLOORWATCH_FCM_CREDENTIALS_JSON` on the rules-engine service (or mount the
   file and set `FLOORWATCH_FCM_CREDENTIALS_PATH`) and redeploy. It must be the
   **same** Firebase project as the app's. `/healthz` then reports
   `"fcm_ready": true`.
4. Log in on the phone once: it registers its device token and the employee's
   channel switches to push; the next assignment arrives as a notification.
5. **iOS** additionally needs `ios/Runner/GoogleService-Info.plist` and an
   APNs Auth Key (Apple Developer account) uploaded under Firebase Project
   settings → Cloud Messaging. `UIBackgroundModes: remote-notification` is
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
