import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../screens/supervisor_home_screen.dart';
import '../screens/task_list_screen.dart';
import 'api_client.dart';
import 'token_storage.dart';

/// Push notifications — receives the SAME notifications the SMS/Twilio
/// path sends today, once an employee's channel is "fcm" (see
/// employee_directory.py's set_channel(), auto-applied on first device-
/// token registration by POST /api/employee/device-token).
///
/// The Firebase settings (API key included) live in the backend's Railway
/// variables and are fetched here after login (GET /api/employee/app-config).
/// They're also cached on the device so FloorwatchApplication.kt (Android) can
/// start Firebase on every process start — including when Android launches the
/// app just to deliver a push while it's closed, where no Dart runs.
///
/// Server side needs, on Railway: FIREBASE_API_KEY (or
/// FLOORWATCH_FIREBASE_API_KEY) for the app, and FLOORWATCH_FCM_CREDENTIALS_JSON
/// (service-account key) for the backend to send. If the key isn't set,
/// initialize() returns quietly and the app runs without push.
class PushService {
  PushService._();
  static final PushService instance = PushService._();

  /// MaterialApp uses this so a push that arrives while the app is open can
  /// show a banner (the OS only draws notifications for a backgrounded app).
  static final GlobalKey<ScaffoldMessengerState> messengerKey = GlobalKey<ScaffoldMessengerState>();

  /// MaterialApp's own navigatorKey — lets a tapped notification jump
  /// straight to My Tasks (see _openFromMessage below) from wherever a
  /// tap happens to resume the app, without every screen needing to know
  /// about push.
  static final GlobalKey<NavigatorState> navigatorKey = GlobalKey<NavigatorState>();

  FirebaseMessaging get _messaging => FirebaseMessaging.instance;
  bool _initialized = false;

  Future<void> initialize() async {
    if (_initialized) return;
    final config = await ApiClient.instance.fetchFirebaseConfig();
    if (config == null) return; // push not configured server-side
    // Cache for the native side (read on the NEXT process start).
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('firebase_api_key', config['apiKey']!);
    await prefs.setString('firebase_app_id', config['appId']!);
    await prefs.setString('firebase_project_id', config['projectId']!);
    await prefs.setString('firebase_sender_id', config['messagingSenderId']!);
    // If the native side already started a default app from the cache this
    // reuses it; if the settings changed since, keep using the running app.
    try {
      await Firebase.initializeApp(
        options: FirebaseOptions(
          apiKey: config['apiKey']!,
          appId: config['appId']!,
          messagingSenderId: config['messagingSenderId']!,
          projectId: config['projectId']!,
        ),
      );
    } catch (_) {
      if (Firebase.apps.isEmpty) rethrow;
    }
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

    // The user tapped a notification to open/resume the app — land on My
    // Tasks (not whatever screen the app would otherwise cold-start to),
    // since that's what they tapped to see. Covers both cases: app was
    // backgrounded (onMessageOpenedApp) and app was fully closed
    // (getInitialMessage, checked once here at startup).
    FirebaseMessaging.onMessageOpenedApp.listen(_openFromMessage);
    final initialMessage = await _messaging.getInitialMessage();
    if (initialMessage != null) await _openFromMessage(initialMessage);
  }

  Future<void> _openFromMessage(RemoteMessage message) async {
    final taskId = message.data['task_id'];
    if (taskId == null || taskId.isEmpty) return;
    // Best-effort — a failed "seen" call must never block navigation.
    try {
      await ApiClient.instance.markTaskSeen(taskId);
    } catch (_) {}
    final nav = navigatorKey.currentState;
    if (nav == null) return;
    final role = await TokenStorage.instance.readRole() ?? 'employee';
    final destination = (role == 'supervisor' || role == 'secondary_admin' || role == 'admin')
        ? const SupervisorHomeScreen(initialTab: 0) // My Tasks, not the Dashboard tab it defaults to
        : const TaskListScreen();
    nav.pushAndRemoveUntil(MaterialPageRoute(builder: (_) => destination), (route) => false);
  }
}
