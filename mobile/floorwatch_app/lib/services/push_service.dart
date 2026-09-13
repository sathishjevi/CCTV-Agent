import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';

import 'api_client.dart';

/// Push notifications — receives the SAME notifications the SMS/Twilio
/// path sends today, once an employee's channel is "fcm" (see
/// employee_directory.py's set_channel(), auto-applied on first device-
/// token registration by POST /api/employee/device-token).
///
/// REQUIRES a real Firebase project before this does anything:
///   1. Create/reuse a Firebase project (the same one backing this
///      repo's FLOORWATCH_FCM_CREDENTIALS_PATH on the backend, so
///      tokens issued here are valid for that backend's Admin SDK).
///   2. Android: add google-services.json to android/app/.
///   3. iOS: add GoogleService-Info.plist to ios/Runner/, plus an APNs
///      Auth Key uploaded to the Firebase project (see
///      dazzling-hopping-comet.md's Phase 4 notes — needs a Mac/Xcode
///      to actually build the iOS target at all).
/// Without those files, Firebase.initializeApp() throws at startup —
/// main.dart catches that and the app still runs, just without push.
class PushService {
  PushService._();
  static final PushService instance = PushService._();

  final _messaging = FirebaseMessaging.instance;
  bool _initialized = false;

  Future<void> initialize() async {
    if (_initialized) return;
    await Firebase.initializeApp();
    _initialized = true;

    // iOS/web require an explicit permission prompt; Android grants
    // silently pre-13, and prompts automatically on 13+ — requestPermission()
    // is the correct call on every platform either way.
    await _messaging.requestPermission(alert: true, badge: true, sound: true);

    final token = await _messaging.getToken();
    if (token != null) {
      await ApiClient.instance.registerDeviceToken(token);
    }
    // A token can rotate (app reinstall, OS-level refresh) — re-register
    // whenever that happens, not just once at startup.
    _messaging.onTokenRefresh.listen((newToken) {
      ApiClient.instance.registerDeviceToken(newToken);
    });
  }
}
