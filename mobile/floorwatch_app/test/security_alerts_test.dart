import 'package:floorwatch_app/models/admin.dart';
import 'package:floorwatch_app/models/security.dart';
import 'package:floorwatch_app/screens/security_alerts_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

Map<String, dynamic> _alertJson({
  String id = 'a1',
  String kind = 'break_in_door',
  String severity = 'critical',
  String status = 'open',
  String zone = 'Back Door',
  bool stillPresent = true,
  String? ackBy,
}) =>
    {
      'alert_id': id,
      'kind': kind,
      'severity': severity,
      'zone_name': zone,
      'message': 'Possible break-in: a person was detected at $zone (door) while the venue is closed.',
      'status': status,
      'created_at': '2026-11-03T02:00:00+00:00',
      'still_present': stillPresent,
      'acknowledged_by': ackBy,
    };

Map<String, dynamic> _statusJson({
  String phase = 'armed',
  List<Map<String, dynamic>> alerts = const [],
  bool shadow = false,
  String? scheduleError,
  bool scheduleConfigured = true,
  String? disarmedBy,
}) =>
    {
      'enabled': true,
      'phase': phase,
      'armed': phase == 'armed',
      'disarmed_until': disarmedBy == null ? null : '2026-11-03T04:00:00+00:00',
      'disarmed_by': disarmedBy,
      'schedule': {'configured': scheduleConfigured, 'timezone': 'UTC'},
      'schedule_error': scheduleError,
      'shadow_mode': shadow,
      'alerts': alerts,
      'occupied_zones': [
        {'zone_id': 'back_door', 'zone_name': 'Back Door', 'since': '2026-11-03T02:00:00+00:00'}
      ],
    };

class _Calls {
  final acked = <String>[];
  final resolved = <List<String>>[];
  final disarmed = <List<Object>>[];
  var armed = 0;
}

Widget _host(SecurityStatus status, _Calls calls) => MaterialApp(
      home: Scaffold(
        body: SecurityAlertsView(
          status: status,
          onAcknowledge: (id) async => calls.acked.add(id),
          onResolve: (id, outcome, note) async => calls.resolved.add([id, outcome, note]),
          onDisarm: (minutes, reason) async => calls.disarmed.add([minutes, reason]),
          onArm: () async => calls.armed++,
        ),
      ),
    );

void main() {
  group('models', () {
    test('parse a full status', () {
      final s = SecurityStatus.fromJson(_statusJson(alerts: [_alertJson(), _alertJson(id: 'a2', status: 'acknowledged', ackBy: 'boss')]));
      expect(s.phase, 'armed');
      expect(s.alerts.length, 2);
      expect(s.openCount, 1); // only the open one counts toward the bell
      expect(s.alerts.first.isCritical, isTrue);
      expect(s.alerts.first.label, 'Possible break-in (door)');
      expect(s.occupiedZones, ['Back Door']);
    });

    test('tolerate a minimal or unknown response without throwing', () {
      final s = SecurityStatus.fromJson({});
      expect(s.alerts, isEmpty);
      expect(s.phase, 'unknown');
      expect(s.openCount, 0);
      final a = SecurityAlert.fromJson({'alert_id': 'x', 'kind': 'something_new'});
      expect(a.label, 'something_new'); // unknown kinds show as-is rather than hiding the alert
    });

    test('a zone without the new fields defaults to no rule', () {
      final z = ZoneRecord.fromJson({'zone_id': 'lobby', 'name': 'Lobby'});
      expect(z.access, 'open');
      expect(z.entryPoint, '');
      final d = ZoneRecord.fromJson({'zone_id': 'door', 'name': 'Door', 'access': 'closed_hours', 'entry_point': 'door'});
      expect((d.access, d.entryPoint), ('closed_hours', 'door'));
    });
  });

  group('SecurityAlertsView', () {
    testWidgets('shows the armed state, the alert and what it means', (tester) async {
      await tester.pumpWidget(_host(SecurityStatus.fromJson(_statusJson(alerts: [_alertJson()])), _Calls()));
      expect(find.text('ARMED: venue closed'), findsOneWidget);
      expect(find.text('Possible break-in (door)'), findsOneWidget);
      expect(find.text('CRITICAL'), findsOneWidget);
      expect(find.textContaining('Back Door · person still detected'), findsOneWidget);
      expect(find.textContaining('nothing more'), findsOneWidget); // the "a detection is not proof" reminder
      expect(find.textContaining('Currently detected in: Back Door'), findsOneWidget);
    });

    testWidgets('says plainly when there is nothing open', (tester) async {
      await tester.pumpWidget(_host(SecurityStatus.fromJson(_statusJson(phase: 'open')), _Calls()));
      expect(find.text('No open alerts.'), findsOneWidget);
      expect(find.text('Venue open'), findsOneWidget);
    });

    testWidgets("\"I'm on it\" acknowledges, and disappears once someone has", (tester) async {
      final calls = _Calls();
      await tester.pumpWidget(_host(SecurityStatus.fromJson(_statusJson(alerts: [_alertJson()])), calls));
      await tester.tap(find.text("I'm on it"));
      await tester.pump();
      expect(calls.acked, ['a1']);

      await tester.pumpWidget(_host(
          SecurityStatus.fromJson(_statusJson(alerts: [_alertJson(status: 'acknowledged', ackBy: 'boss')])), calls));
      expect(find.text("I'm on it"), findsNothing);
      expect(find.textContaining('acknowledged by boss'), findsOneWidget);
    });

    testWidgets('closing an alert asks what was found and sends the outcome and note', (tester) async {
      final calls = _Calls();
      await tester.pumpWidget(_host(SecurityStatus.fromJson(_statusJson(alerts: [_alertJson()])), calls));
      await tester.tap(find.text('Close alert…'));
      await tester.pumpAndSettle();
      expect(find.text('What did you find?'), findsOneWidget);

      await tester.tap(find.text('False alarm'));
      await tester.enterText(find.byType(TextField), '  It was the cat  ');
      await tester.tap(find.text('Close alert'));
      await tester.pumpAndSettle();
      expect(calls.resolved, [
        ['a1', 'false_alarm', 'It was the cat'] // outcome as chosen, note trimmed
      ]);
    });

    testWidgets('cancelling the close dialog does nothing', (tester) async {
      final calls = _Calls();
      await tester.pumpWidget(_host(SecurityStatus.fromJson(_statusJson(alerts: [_alertJson()])), calls));
      await tester.tap(find.text('Close alert…'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();
      expect(calls.resolved, isEmpty);
    });

    testWidgets('disarming warns what it does, then sends the duration and reason', (tester) async {
      final calls = _Calls();
      await tester.pumpWidget(_host(SecurityStatus.fromJson(_statusJson()), calls));
      await tester.tap(find.textContaining('Disarm…'));
      await tester.pumpAndSettle();
      expect(find.textContaining('It is recorded and the team is told'), findsOneWidget);
      await tester.enterText(find.byType(TextField), 'cleaning crew');
      await tester.tap(find.text('Disarm'));
      await tester.pumpAndSettle();
      expect(calls.disarmed, [
        [60, 'cleaning crew']
      ]);
    });

    testWidgets('while disarmed it says who and offers to re-arm', (tester) async {
      final calls = _Calls();
      await tester.pumpWidget(_host(SecurityStatus.fromJson(_statusJson(phase: 'disarmed', disarmedBy: 'boss')), calls));
      expect(find.text('DISARMED by a supervisor'), findsOneWidget);
      expect(find.textContaining('Disarmed by boss'), findsOneWidget);
      expect(find.textContaining('Disarm…'), findsNothing);
      await tester.tap(find.text('Re-arm now'));
      await tester.pump();
      expect(calls.armed, 1);
    });

    testWidgets('configuration problems are shown, not hidden', (tester) async {
      await tester.pumpWidget(_host(
          SecurityStatus.fromJson(_statusJson(phase: 'unscheduled', scheduleConfigured: false, shadow: true)), _Calls()));
      expect(find.textContaining('No opening hours set: only off-limits areas can alert.'), findsOneWidget);
      expect(find.textContaining('Shadow mode is ON'), findsOneWidget);

      await tester.pumpWidget(_host(
          SecurityStatus.fromJson(_statusJson(phase: 'unscheduled', scheduleConfigured: false, scheduleError: "in the rule 'x': bad")),
          _Calls()));
      expect(find.textContaining("Opening hours could not be read: in the rule 'x': bad"), findsOneWidget);
    });
  });
}
