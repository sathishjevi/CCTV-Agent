// Security alerts (break-in, after-hours, off-limits area, someone hiding after close) — the shapes returned
// by GET /api/employee/dashboard/security/status. See services/floorwatch-rules-engine/app/security_engine.py.

const securityKindLabels = {
  'break_in_door': 'Possible break-in (door)',
  'break_in_window': 'Possible break-in (window)',
  'after_hours_presence': 'Person here while closed',
  'restricted_zone_entry': 'Person in an off-limits area',
  'hiding_after_close': 'Someone still inside after closing',
};

const securityPhaseText = {
  'open': 'Venue open',
  'closing_grace': 'Closed: locking-up grace period (not armed yet)',
  'opening_grace': 'Opening soon: grace period (not armed)',
  'armed': 'ARMED: venue closed',
  'disarmed': 'DISARMED by a supervisor',
  'unscheduled': 'No opening hours set',
  'off': 'Security alerts are off',
  'unknown': 'Unknown',
};

const securityOutcomeLabels = {
  'handled': 'Handled',
  'false_alarm': 'False alarm',
  'authorised': 'Authorised person',
};

class SecurityAlert {
  final String alertId;
  final String kind;
  final String severity; // critical | high | medium
  final String zoneName;
  final String message;
  final String status; // open | acknowledged
  final String createdAt;
  final bool stillPresent;
  final String? acknowledgedBy;

  SecurityAlert({
    required this.alertId,
    required this.kind,
    required this.severity,
    required this.zoneName,
    required this.message,
    required this.status,
    required this.createdAt,
    required this.stillPresent,
    required this.acknowledgedBy,
  });

  bool get isOpen => status == 'open';
  bool get isCritical => severity == 'critical';
  String get label => securityKindLabels[kind] ?? kind;

  factory SecurityAlert.fromJson(Map<String, dynamic> json) {
    return SecurityAlert(
      alertId: json['alert_id'] as String,
      kind: json['kind'] as String? ?? '',
      severity: json['severity'] as String? ?? 'high',
      zoneName: json['zone_name'] as String? ?? json['zone_id'] as String? ?? '',
      message: json['message'] as String? ?? '',
      status: json['status'] as String? ?? 'open',
      createdAt: json['created_at'] as String? ?? '',
      stillPresent: json['still_present'] as bool? ?? false,
      acknowledgedBy: json['acknowledged_by'] as String?,
    );
  }
}

class SecurityStatus {
  final bool enabled;
  final String phase;
  final bool armed;
  final String? disarmedUntil;
  final String? disarmedBy;
  final bool scheduleConfigured;
  final String? scheduleError;
  final bool shadowMode;
  final List<SecurityAlert> alerts;
  final List<String> occupiedZones;

  SecurityStatus({
    required this.enabled,
    required this.phase,
    required this.armed,
    required this.disarmedUntil,
    required this.disarmedBy,
    required this.scheduleConfigured,
    required this.scheduleError,
    required this.shadowMode,
    required this.alerts,
    required this.occupiedZones,
  });

  int get openCount => alerts.where((a) => a.isOpen).length;
  bool get isDisarmed => phase == 'disarmed';

  factory SecurityStatus.fromJson(Map<String, dynamic> json) {
    final schedule = (json['schedule'] as Map<String, dynamic>?) ?? const {};
    return SecurityStatus(
      enabled: json['enabled'] as bool? ?? false,
      phase: json['phase'] as String? ?? 'unknown',
      armed: json['armed'] as bool? ?? false,
      disarmedUntil: json['disarmed_until'] as String?,
      disarmedBy: json['disarmed_by'] as String?,
      scheduleConfigured: schedule['configured'] as bool? ?? false,
      scheduleError: json['schedule_error'] as String?,
      shadowMode: json['shadow_mode'] as bool? ?? false,
      alerts: ((json['alerts'] as List<dynamic>?) ?? const [])
          .map((e) => SecurityAlert.fromJson(e as Map<String, dynamic>))
          .toList(),
      occupiedZones: ((json['occupied_zones'] as List<dynamic>?) ?? const [])
          .map((e) => (e as Map<String, dynamic>)['zone_name'] as String? ?? '')
          .toList(),
    );
  }
}
