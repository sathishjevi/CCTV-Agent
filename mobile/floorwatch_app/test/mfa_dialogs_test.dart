import 'package:floorwatch_app/screens/mfa_dialogs.dart';
import 'package:floorwatch_app/services/api_client.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// A page with one button that opens [open] and shows what it returned.
Widget _host(Future<Object?> Function(BuildContext) open, ValueNotifier<Object?> result) {
  return MaterialApp(
    home: Builder(
      builder: (context) => Scaffold(
        body: Center(
          child: ElevatedButton(
            onPressed: () async => result.value = await open(context),
            child: const Text('open'),
          ),
        ),
      ),
    ),
  );
}

void main() {
  group('promptForMfaCode', () {
    testWidgets('closes with the server result when the code is accepted', (tester) async {
      final result = ValueNotifier<Object?>(null);
      String? sent;
      await tester.pumpWidget(_host(
          (c) => promptForMfaCode<String>(c, verify: (code) async {
                sent = code;
                return 'signed-in';
              }),
          result));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField), ' 123456 ');
      await tester.tap(find.text('Verify'));
      await tester.pumpAndSettle();

      expect(sent, '123456'); // trimmed
      expect(result.value, 'signed-in');
      expect(find.byType(AlertDialog), findsNothing);
    });

    testWidgets('stays open and shows the message when the code is wrong, then accepts a retry', (tester) async {
      final result = ValueNotifier<Object?>(null);
      var attempts = 0;
      await tester.pumpWidget(_host(
          (c) => promptForMfaCode<String>(c, verify: (code) async {
                attempts++;
                if (code != '222222') throw ApiException(401, "That code didn't work.");
                return 'ok';
              }),
          result));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField), '111111');
      await tester.tap(find.text('Verify'));
      await tester.pumpAndSettle();
      expect(find.text("That code didn't work."), findsOneWidget);
      expect(find.byType(AlertDialog), findsOneWidget); // still there
      expect(result.value, isNull);

      await tester.enterText(find.byType(TextField), '222222');
      await tester.tap(find.text('Verify'));
      await tester.pumpAndSettle();
      expect(attempts, 2);
      expect(result.value, 'ok');
    });

    testWidgets('cancel returns null and never calls the server', (tester) async {
      final result = ValueNotifier<Object?>('unset');
      var called = false;
      await tester.pumpWidget(_host(
          (c) => promptForMfaCode<String>(c, verify: (code) async {
                called = true;
                return 'x';
              }),
          result));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();
      expect(result.value, isNull);
      expect(called, isFalse);
    });

    testWidgets('an empty code is not sent', (tester) async {
      var called = false;
      await tester.pumpWidget(_host(
          (c) => promptForMfaCode<String>(c, verify: (code) async {
                called = true;
                return 'x';
              }),
          ValueNotifier<Object?>(null)));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Verify'));
      await tester.pumpAndSettle();
      expect(called, isFalse);
    });

    testWidgets('a network failure is reported, not swallowed', (tester) async {
      await tester.pumpWidget(_host(
          (c) => promptForMfaCode<String>(c, verify: (code) async => throw Exception('socket closed')),
          ValueNotifier<Object?>(null)));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField), '123456');
      await tester.tap(find.text('Verify'));
      await tester.pumpAndSettle();
      expect(find.textContaining('Network error'), findsOneWidget);
    });
  });

  group('runMfaEnrollment', () {
    testWidgets('shows the key, takes the first code, then shows the recovery codes once', (tester) async {
      final result = ValueNotifier<Object?>(null);
      await tester.pumpWidget(_host(
          (c) => runMfaEnrollment(
                c,
                begin: () async => {'secret': 'ABCDEFGHIJKLMNOP', 'otpauth_uri': 'otpauth://totp/x'},
                confirm: (code) async => {
                  'recovery_codes': ['aaaa-bbbb-cccc-dddd', 'eeee-ffff-0000-1111'],
                  'token': 'session-token',
                },
              ),
          result));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();

      expect(find.text('ABCD EFGH IJKL MNOP'), findsOneWidget); // the key, in groups of four
      await tester.enterText(find.byType(TextField), '654321');
      await tester.tap(find.text('Verify'));
      await tester.pumpAndSettle();

      expect(find.text('Save your recovery codes'), findsOneWidget);
      expect(find.textContaining('aaaa-bbbb-cccc-dddd'), findsOneWidget);
      await tester.tap(find.text("I've saved them"));
      await tester.pumpAndSettle();

      // the caller (a sign-in) gets the whole server response, token included
      expect((result.value as Map)['token'], 'session-token');
    });

    testWidgets('cancelling the code step abandons enrolment', (tester) async {
      final result = ValueNotifier<Object?>('unset');
      await tester.pumpWidget(_host(
          (c) => runMfaEnrollment(c,
              begin: () async => {'secret': 'ABCDEFGH', 'otpauth_uri': ''},
              confirm: (code) async => {'recovery_codes': <String>[]}),
          result));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();
      expect(result.value, isNull);
    });

    testWidgets('if the server refuses to start enrolment it says why and returns null', (tester) async {
      final result = ValueNotifier<Object?>('unset');
      await tester.pumpWidget(_host(
          (c) => runMfaEnrollment(c,
              begin: () async => throw ApiException(409, 'Two-step verification is already on.'),
              confirm: (code) async => {}),
          result));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      expect(find.text('Two-step verification is already on.'), findsOneWidget);
      expect(result.value, isNull);
    });
  });
}
