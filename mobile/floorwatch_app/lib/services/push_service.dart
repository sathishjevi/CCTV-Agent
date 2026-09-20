import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/material.dart';

import 'api_client.dart';

/// Push notifications — receives the SAME notifications the SMS/Twilio
/// path sends today, once an employee's channel is "fcm" (see
/// employee_directory.py's set_channel(), auto-applied on first device-
/// token registration by POST /api/employee/device-token).
///
/// Firebase must be set up BY THE BUILD (google-services.json), not from
/// Dart at runtime: when a push arrives for a closed app, Android starts the
/// process without running any Dart, so a default FirebaseApp has to already
/// exist natively or the notification can't be displayed. The key is kept out
/// of git — see README ("Push notifications"): a committed template plus
/// FIREBASE_API_KEY supplied at build time.
///
/// Without that, Firebase.initializeApp() throws — callers catch it and the
/// app simply runs without push.
class PushService {
  PushService._();
  static final PushService instance = PushService._();

  /// MaterialApp uses this so a push that arrives while the app is open can
  /// show a banner (the OS only draws notifications for a backgrounded app).
  static final GlobalKey<ScaffoldMessengerState> messengerKey = GlobalKey<ScaffoldMessengerState>();

  FirebaseMessaging get _messaging => FirebaseMessaging.instance;
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
