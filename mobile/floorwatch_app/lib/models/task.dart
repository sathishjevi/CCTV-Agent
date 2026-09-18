/// Mirrors the shape returned by GET /api/employee/tasks in
/// services/floorwatch-rules-engine/app/main.py's employee_tasks()
/// endpoint — keep these two in sync if that endpoint's fields change.
class EmployeeTask {
  final String taskId;
  final String taskName;
  final String taskType;
  final String zoneId;
  final String zoneName;
  final double assignedMinutes;
  final double activeMinutes;
  final double elapsedMinutes;
  final String workflowStatus;
  final String shortCode;

  EmployeeTask({
    required this.taskId,
    required this.taskName,
    required this.taskType,
    required this.zoneId,
    required this.zoneName,
    required this.assignedMinutes,
    required this.activeMinutes,
    required this.elapsedMinutes,
    required this.workflowStatus,
    required this.shortCode,
  });

  factory EmployeeTask.fromJson(Map<String, dynamic> json) {
    return EmployeeTask(
      taskId: json['task_id'] as String,
      taskName: json['task_name'] as String,
      taskType: json['task_type'] as String? ?? '',
      zoneId: json['zone_id'] as String,
      zoneName: json['zone_name'] as String? ?? json['zone_id'] as String,
      assignedMinutes: (json['assigned_minutes'] as num).toDouble(),
      activeMinutes: (json['active_minutes'] as num).toDouble(),
      elapsedMinutes: (json['elapsed_minutes'] as num).toDouble(),
      workflowStatus: json['workflow_status'] as String? ?? 'unassigned',
      shortCode: json['short_code'] as String? ?? '',
    );
  }

  /// Human-readable label for the workflow_status values defined in
  /// effort_engine.py's WORKFLOW_STATUSES — kept in one place so the
  /// list and detail screens show the exact same wording.
  String get statusLabel {
    switch (workflowStatus) {
      case 'unassigned':
        return 'Unassigned';
      case 'notified':
        return 'Waiting for you to start';
      case 'notify_failed':
        return 'Notification failed';
      case 'in_progress':
        return 'In progress';
      case 'awaiting_update':
        return 'Status update needed';
      case 'extension_requested':
        return 'Extension requested';
      case 'review_requested':
        return 'Review requested';
      case 'completed':
        return 'Completed';
      default:
        return workflowStatus;
    }
  }

  // Mirrors mark_started()'s exact guard in effort_engine.py — keep in
  // sync if that ever changes, or Start will show as available (or
  // hidden) in cases the backend actually disagrees with.
  bool get canStart =>
      workflowStatus == 'notified' || workflowStatus == 'notify_failed' || workflowStatus == 'awaiting_update';
  bool get isActionable => workflowStatus != 'completed';
}
