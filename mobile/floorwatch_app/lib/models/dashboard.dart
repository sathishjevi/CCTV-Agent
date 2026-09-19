import 'admin.dart';

/// Models for the supervisor mobile "Dashboard" tab
/// (/api/employee/dashboard/*), mirroring the exact shapes the web
/// dashboard's own /api/state, /api/tasks and /api/queue/tasks return
/// — see main.py's _refresh_snapshots()/pending_flags().
class ZoneState {
  final String zoneId;
  final String status; // covered | gap | nudge | escalated (see engine.py)

  ZoneState({required this.zoneId, required this.status});

  factory ZoneState.fromJson(String zoneId, Map<String, dynamic> json) {
    return ZoneState(zoneId: zoneId, status: json['status'] as String? ?? 'covered');
  }
}

class DashboardTask {
  final String taskId;
  final String taskName;
  final String zoneId;
  final String zoneName;
  final double assignedMinutes;
  final double activeMinutes;
  final double elapsedMinutes;
  final String status; // open | flagged | resolved
  final String workflowStatus;
  final String? assignedTo;
  final bool reopenedForReview;

  DashboardTask({
    required this.taskId,
    required this.taskName,
    required this.zoneId,
    required this.zoneName,
    required this.assignedMinutes,
    required this.activeMinutes,
    required this.elapsedMinutes,
    required this.status,
    required this.workflowStatus,
    required this.assignedTo,
    required this.reopenedForReview,
  });

  bool get isOpen => status == 'open';

  factory DashboardTask.fromJson(String taskId, Map<String, dynamic> json) {
    return DashboardTask(
      taskId: taskId,
      taskName: json['task_name'] as String? ?? '',
      zoneId: json['zone_id'] as String? ?? '',
      zoneName: json['zone_name'] as String? ?? json['zone_id'] as String? ?? '',
      assignedMinutes: (json['assigned_minutes'] as num?)?.toDouble() ?? 0,
      activeMinutes: (json['active_minutes'] as num?)?.toDouble() ?? 0,
      elapsedMinutes: (json['elapsed_minutes'] as num?)?.toDouble() ?? 0,
      status: json['status'] as String? ?? 'open',
      workflowStatus: json['workflow_status'] as String? ?? 'unassigned',
      assignedTo: json['assigned_to'] as String?,
      reopenedForReview: json['reopened_for_review'] as bool? ?? false,
    );
  }
}

/// One entry from the Supervisor Queue — a task needing attention, of
/// any origin. `kind` decides which action applies; see
/// effort_engine.py's pending_flags() docstring for the exact meaning
/// of each kind.
class QueueItem {
  final String taskId;
  final String taskName;
  final String zoneName;
  final double activeMinutes;
  final double elapsedMinutes;
  final double assignedMinutes;
  final String? assignedTo;
  final String kind; // flagged | extension_requested | review_requested

  QueueItem({
    required this.taskId,
    required this.taskName,
    required this.zoneName,
    required this.activeMinutes,
    required this.elapsedMinutes,
    required this.assignedMinutes,
    required this.assignedTo,
    required this.kind,
  });

  factory QueueItem.fromJson(Map<String, dynamic> json) {
    return QueueItem(
      taskId: json['task_id'] as String,
      taskName: json['task_name'] as String? ?? '',
      zoneName: json['zone_name'] as String? ?? json['zone_id'] as String? ?? '',
      activeMinutes: (json['active_minutes'] as num?)?.toDouble() ?? 0,
      elapsedMinutes: (json['elapsed_minutes'] as num?)?.toDouble() ?? 0,
      assignedMinutes: (json['assigned_minutes'] as num?)?.toDouble() ?? 0,
      assignedTo: json['assigned_to'] as String?,
      kind: json['kind'] as String? ?? 'flagged',
    );
  }
}

/// A coverage-gap directive the system drafted and is waiting on a
/// supervisor to approve (send) or reassign — /api/queue's items, the
/// zone half of the web dashboard's Supervisor Queue.
class ZoneDirective {
  final String zoneId;
  final String zoneName;
  final String message;

  ZoneDirective({required this.zoneId, required this.zoneName, required this.message});

  factory ZoneDirective.fromJson(Map<String, dynamic> json) {
    final zoneId = json['zone_id'] as String? ?? '';
    return ZoneDirective(
      zoneId: zoneId,
      zoneName: json['zone_name'] as String? ?? zoneId,
      message: json['message'] as String? ?? '',
    );
  }
}

/// The web dashboard's four header counters. It counts events since the
/// page loaded; here they're derived from the durable history (most recent
/// events), so they survive an app restart. Same counting rules as the web:
///  - nudges: zone nudges + low-effort task nudges
///  - effort flags: distinct tasks flagged, minus ones dismissed as false alarms
///  - resolved: flagged tasks a supervisor closed out via "reviewed"
///  - supervisor actions: zones a supervisor resolved + distinct tasks a
///    supervisor confirmed/reviewed (dismissing doesn't count)
class DashboardStats {
  final int zonesCovered;
  final int nudges;
  final int effortFlags;
  final int effortFlagsResolved;
  final int supervisorActions;

  DashboardStats({
    required this.zonesCovered,
    required this.nudges,
    required this.effortFlags,
    required this.effortFlagsResolved,
    required this.supervisorActions,
  });

  factory DashboardStats.from({required int zonesCovered, required List<HistoryEvent> newestFirst}) {
    var nudges = 0;
    var zoneActions = 0;
    final flagged = <String>{};
    final resolved = <String>{};
    final taskActions = <String>{};
    bool bySupervisor(HistoryEvent e) => (e.resolvedBy ?? '').startsWith('supervisor:');
    for (final e in newestFirst.reversed) {
      final id = e.taskId ?? '';
      switch (e.eventType) {
        case 'zone_nudge_sent':
        case 'task_low_effort_nudge':
          nudges++;
          break;
        case 'zone_resolved':
          if (bySupervisor(e)) zoneActions++;
          break;
        case 'task_flag':
          flagged.add(id);
          break;
        case 'task_flag_confirmed':
          if (bySupervisor(e)) taskActions.add(id);
          break;
        case 'task_resolved':
          if (e.actionType == 'dismissed') {
            flagged.remove(id);
          } else if (e.actionType == 'reviewed') {
            if (bySupervisor(e)) taskActions.add(id);
            resolved.add(id);
          }
          break;
      }
    }
    return DashboardStats(
      zonesCovered: zonesCovered,
      nudges: nudges,
      effortFlags: flagged.length,
      effortFlagsResolved: resolved.length,
      supervisorActions: zoneActions + taskActions.length,
    );
  }
}
