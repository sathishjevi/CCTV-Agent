import 'package:flutter/foundation.dart';
import 'package:posthog_flutter/posthog_flutter.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Crash and error reporting to PostHog — the same project the backend reports
/// to, so an app crash and the server error behind it sit side by side.
///
/// Error tracking ONLY. PostHog's SDK also does analytics, session replay,
/// surveys and feature flags; every one of those is switched off below, and
/// [onlyExceptions] is a hard backstop: whatever a default or a future SDK
/// version turns on, nothing except an `$exception` event ever leaves the phone.
/// No employee number, name or phone is attached — events carry PostHog's
/// anonymous per-install id only, and we never call identify().
///
/// The project token is a Railway variable, not part of the build (same
/// approach as the Firebase settings): the server hands it to the app after
/// login (GET /api/employee/app-config) and the app caches it so crashes on the
/// NEXT launch — including before login — are reported too. If the server stops
/// sending it, the cache is cleared and reporting stops.
///
/// Off in debug builds, so a developer's emulator doesn't fill the production
/// project with noise (opt in with --dart-define=FLOORWATCH_REPORT_IN_DEBUG=true
/// when testing this itself). Everything here is best-effort and never throws:
/// reporting a crash must not cause one.
class CrashReporting {
  CrashReporting._();

  static const _keyToken = 'posthog_project_token';
  static const _keyHost = 'posthog_host';
  static bool _started = false;
  static const _reportInDebug = bool.fromEnvironment('FLOORWATCH_REPORT_IN_DEBUG');
  static bool get _off => kDebugMode && !_reportInDebug;

  /// Call once at startup, before runApp: starts reporting from the token
  /// cached on the last login, if there is one.
  static Future<void> startFromCache() async {
    if (_off) return;
    try {
      final prefs = await SharedPreferences.getInstance();
      final token = prefs.getString(_keyToken);
      if (token == null || token.isEmpty) return;
      await _start(token, prefs.getString(_keyHost));
    } catch (e) {
      debugPrint('CrashReporting: could not start from cache: $e');
    }
  }

  /// Call with the `posthog` block from /api/employee/app-config (null when the
  /// server has no token set): caches it and starts reporting now.
  static Future<void> configure(Object? posthogConfig) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      if (posthogConfig is! Map || (posthogConfig['apiKey'] as String? ?? '').isEmpty) {
        await prefs.remove(_keyToken);
        await prefs.remove(_keyHost);
        return;
      }
      final token = posthogConfig['apiKey'] as String;
      final host = posthogConfig['host'] as String?;
      await prefs.setString(_keyToken, token);
      if (host != null && host.isNotEmpty) await prefs.setString(_keyHost, host);
      if (_off) return;
      await _start(token, host);
    } catch (e) {
      debugPrint('CrashReporting: could not configure: $e');
    }
  }

  static Future<void> _start(String token, String? host) async {
    if (_started) return;
    final config = PostHogConfig(token)
      ..captureApplicationLifecycleEvents = false
      ..preloadFeatureFlags = false
      ..sendFeatureFlagEvents = false
      ..surveys = false
      ..sessionReplay = false
      ..personProfiles = PostHogPersonProfiles.never
      ..capturePushNotificationSubscriptions = false
      ..capturePushNotificationOpened = false
      ..beforeSend = [onlyExceptions];
    if (host != null && host.isNotEmpty) config.host = host;
    config.errorTrackingConfig
      ..captureFlutterErrors = true
      ..capturePlatformDispatcherErrors = true
      ..captureNativeExceptions = true;
    await Posthog().setup(config);
    await Posthog().register('service', 'mobile-app');
    _started = true;
  }

  /// Hard privacy backstop — see the class comment.
  @visibleForTesting
  static PostHogEvent? onlyExceptions(PostHogEvent event) => event.event == r'$exception' ? event : null;

  /// For failures the app deliberately swallows so it keeps working (push setup
  /// failing must never block a login) but that someone still needs to hear
  /// about. [where] says which step, e.g. 'push_init'.
  static void report(Object error, StackTrace? stackTrace, {required String where}) {
    if (!_started) return;
    try {
      Posthog().captureException(error: error, stackTrace: stackTrace, properties: {'where': where});
    } catch (_) {}
  }
}
