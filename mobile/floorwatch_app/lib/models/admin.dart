/// Models for the mobile app's admin sections (Manage Employees, Manage
/// Zones, Manage Users, History) — /api/employee/dashboard/* and
/// /api/employee/admin/* in main.py, mirroring the web dashboard's own
/// /api/admin/* shapes exactly.
class EmployeeRecord {
  final String employeeNumber;
  final String name;
  final String role; // employee | supervisor | admin
  final String department;
  final String phone;
  final bool active;
  final bool isPrimaryContact;

  EmployeeRecord({
    required this.employeeNumber,
    required this.name,
    required this.role,
    required this.department,
    required this.phone,
    required this.active,
    required this.isPrimaryContact,
  });

  factory EmployeeRecord.fromJson(Map<String, dynamic> json) {
    return EmployeeRecord(
      employeeNumber: json['employee_number'] as String,
      name: json['name'] as String? ?? '',
      role: json['role'] as String? ?? 'employee',
      department: json['department'] as String? ?? '',
      phone: json['phone'] as String? ?? '',
      active: json['active'] as bool? ?? true,
      isPrimaryContact: json['is_primary_contact'] as bool? ?? false,
    );
  }
}

class ZoneRecord {
  final String zoneId;
  final String name;
  final String roleTag;
  final String? cameraId;
  final bool active;
  final bool staffed;

  ZoneRecord({
    required this.zoneId,
    required this.name,
    required this.roleTag,
    required this.cameraId,
    required this.active,
    required this.staffed,
  });

  factory ZoneRecord.fromJson(Map<String, dynamic> json) {
    return ZoneRecord(
      zoneId: json['zone_id'] as String,
      name: json['name'] as String? ?? '',
      roleTag: json['role_tag'] as String? ?? '',
      cameraId: json['camera_id'] as String?,
      active: json['active'] as bool? ?? true,
      staffed: json['staffed'] as bool? ?? true,
    );
  }
}

class DashboardUserAccount {
  final String username;
  final String role; // admin | supervisor | viewer | service
  final bool active;

  DashboardUserAccount({required this.username, required this.role, required this.active});

  factory DashboardUserAccount.fromJson(Map<String, dynamic> json) {
    return DashboardUserAccount(
      username: json['username'] as String,
      role: json['role'] as String? ?? 'viewer',
      active: json['active'] as bool? ?? true,
    );
  }
}

class HistoryEvent {
  final String eventType;
  final String? actionType;
  final String? message;
  final String? taskId;
  final String? taskName;
  final String? zoneName;
  final String? resolvedBy;
  final String? timestamp;

  HistoryEvent({
    required this.eventType,
    required this.actionType,
    required this.message,
    required this.taskId,
    required this.taskName,
    required this.zoneName,
    required this.resolvedBy,
    required this.timestamp,
  });

  factory HistoryEvent.fromJson(Map<String, dynamic> json) {
    return HistoryEvent(
      eventType: json['event_type'] as String? ?? '',
      actionType: json['action_type'] as String?,
      message: json['message'] as String?,
      taskId: json['task_id'] as String?,
      taskName: json['task_name'] as String?,
      zoneName: json['zone_name'] as String?,
      resolvedBy: json['resolved_by'] as String?,
      timestamp: json['timestamp'] as String?,
    );
  }
}
