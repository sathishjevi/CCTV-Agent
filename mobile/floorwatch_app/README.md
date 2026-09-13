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

`lib/services/push_service.dart` calls `Firebase.initializeApp()`, which
throws until real config is in place. The app still runs without it
(both `main.dart` and `otp_entry_screen.dart` swallow that error) — you
just won't get push until this is done:

1. Create or reuse a Firebase project — **must be the same project**
   backing this repo's `FLOORWATCH_FCM_CREDENTIALS_PATH` on the backend,
   since a token issued to this app has to be valid for that backend's
   Admin SDK to send to.
2. **Android**: Firebase console → Project settings → add an Android app
   (package name `com.floorwatch.floorwatch_app`, matching what
   `flutter create --org com.floorwatch` generated) → download
   `google-services.json` → place it at `android/app/google-services.json`.
3. **iOS**: same console → add an iOS app (bundle id
   `com.floorwatch.floorwatchApp`) → download
   `GoogleService-Info.plist` → place it at `ios/Runner/GoogleService-Info.plist`.
   iOS additionally needs an **APNs Auth Key** uploaded to the Firebase
   project (Firebase console → Project settings → Cloud Messaging → APNs
   Authentication Key) — that key comes from an Apple Developer account.

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
