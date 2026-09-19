import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/material.dart';

import 'api_client.dart';

/// Push notifications — receives the SAME notifications the SMS/Twilio
/// path sends today, once an employee's channel is "fcm" (see
/// employee_directory.py's set_channel(), auto-applied on first device-
/// token registration by POST /api/employee/device-token).
///
/// REQUIRES a real Firebase project before this does anything:
///   1. Create a Firebase project and add an Android app with package name
///      com.floorwatch.floorwatch_app.
///   2. Download its google-services.json into android/app/ and rebuild —
///      android/app/build.gradle.kts applies the Google services plugin
///      automatically when that file exists.
///   3. Backend: Project settings > Service accounts > Generate new private
///      key, then paste the whole JSON into the Railway variable
///      FLOORWATCH_FCM_CREDENTIALS_JSON (or mount it and set
///      FLOORWATCH_FCM_CREDENTIALS_PATH).
///   4. iOS additionally needs GoogleService-Info.plist in ios/Runner/ and an
///      APNs key uploaded to the Firebase project (needs a Mac/Xcode).
/// Without those, Firebase.initializeApp() throws — callers catch that and
/// the app still runs, just without push (SMS/dashboard behave as before).
class PushService {
  PushService._();
  static final PushService instance = PushService._();

  /// MaterialApp uses this so a push that arrives while the app is open can
  /// show a banner (the OS only draws notifications for a backgrounded app).
  static final GlobalKey<ScaffoldMessengerState> messengerKey = GlobalKey<ScaffoldMessengerState>();

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

    // Foreground: show the notification text in-app.
    FirebaseMessaging.onMessage.listen((message) {
      final body = message.notification?.body;
      if (body == null || body.isEmpty) return;
      messengerKey.currentState
        ?..hideCurrentSnackBar()
        ..showSnackBar(SnackBar(content: Text(body), duration: const Duration(seconds: 6)));
    });
  }
}
