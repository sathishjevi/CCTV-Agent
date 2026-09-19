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
