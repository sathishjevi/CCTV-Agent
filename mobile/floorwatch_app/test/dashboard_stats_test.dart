import 'package:flutter_test/flutter_test.dart';

import 'package:floorwatch_app/models/admin.dart';
import 'package:floorwatch_app/models/dashboard.dart';

HistoryEvent event(String type, {String? task, String? action, String? by}) => HistoryEvent(
      eventType: type,
      actionType: action,
      message: null,
      taskId: task,
      taskName: null,
      zoneName: null,
      resolvedBy: by,
      timestamp: null,
    );

void main() {
  // History arrives newest-first; DashboardStats replays it oldest-first.
  test('counts nudges from both zone and low-effort task events', () {
    final stats = DashboardStats.from(zonesCovered: 3, newestFirst: [
      event('task_low_effort_nudge', task: 't1'),
      event('zone_nudge_sent'),
      event('zone_nudge_sent'),
    ]);
    expect(stats.nudges, 3);
    expect(stats.zonesCovered, 3);
  });

  test('a dismissed flag is a false alarm: not counted, and not a supervisor action', () {
    final stats = DashboardStats.from(zonesCovered: 0, newestFirst: [
      event('task_resolved', task: 't1', action: 'dismissed', by: 'supervisor:alice'),
      event('task_flag', task: 't1'),
    ]);
    expect(stats.effortFlags, 0);
    expect(stats.supervisorActions, 0);
  });

  test('confirm then review of one flag is one supervisor action and one resolved flag', () {
    final stats = DashboardStats.from(zonesCovered: 0, newestFirst: [
      event('task_resolved', task: 't1', action: 'reviewed', by: 'supervisor:employee:300'),
      event('task_flag_confirmed', task: 't1', by: 'supervisor:employee:300'),
      event('task_flag', task: 't1'),
    ]);
    expect(stats.effortFlags, 1);
    expect(stats.effortFlagsResolved, 1);
    expect(stats.supervisorActions, 1);
  });

  test('a task flagged twice still counts as one flag; zone resolutions by a supervisor add actions', () {
    final stats = DashboardStats.from(zonesCovered: 0, newestFirst: [
      event('zone_resolved', by: 'supervisor:bob'),
      event('zone_resolved', by: 'auto'),
      event('task_flag', task: 't1'),
      event('task_flag', task: 't1'),
    ]);
    expect(stats.effortFlags, 1);
    expect(stats.supervisorActions, 1);
  });
}
