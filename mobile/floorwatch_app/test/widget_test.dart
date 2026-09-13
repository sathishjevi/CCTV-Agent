import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:floorwatch_app/main.dart';

void main() {
  // flutter_secure_storage talks to the platform over this MethodChannel,
  // which doesn't exist under plain widget tests — without mocking it,
  // TokenStorage.readToken()'s Future never resolves, _StartupGate's
  // loading spinner never clears, and pumpAndSettle() hangs forever
  // waiting for that animation to stop. Returning null here simulates
  // "no token stored yet" (a logged-out cold start), which is exactly
  // the scenario this test is checking.
  const channel = MethodChannel('plugins.it_nomads.com/flutter_secure_storage');
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async => null);
  });

  tearDown(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, null);
  });

  testWidgets('App launches to the phone entry screen when logged out', (WidgetTester tester) async {
    await tester.pumpWidget(const FloorwatchApp());
    await tester.pumpAndSettle();

    expect(find.text('Floorwatch'), findsOneWidget);
    expect(find.text('Send code'), findsOneWidget);
  });
}
