import 'package:floorwatch_app/services/crash_reporting.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:posthog_flutter/posthog_flutter.dart';

void main() {
  group('CrashReporting.onlyExceptions (the privacy backstop)', () {
    test(r'lets an $exception event through unchanged', () {
      final event = PostHogEvent(event: r'$exception', properties: {'where': 'push_init'});
      expect(CrashReporting.onlyExceptions(event), same(event));
    });

    test('drops every analytics-style event, whatever the SDK or its defaults do', () {
      for (final name in [
        'Application Opened',
        'Application Backgrounded',
        r'$screen',
        r'$autocapture',
        r'$feature_flag_called',
        r'$snapshot',
        'Push Notification Opened',
        'anything_else',
      ]) {
        expect(CrashReporting.onlyExceptions(PostHogEvent(event: name)), isNull, reason: name);
      }
    });
  });

  test('report() before start-up is a quiet no-op, never a crash', () {
    expect(() => CrashReporting.report(Exception('x'), StackTrace.current, where: 'test'), returnsNormally);
  });
}
