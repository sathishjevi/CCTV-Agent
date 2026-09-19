import 'dart:convert';
import 'package:http/http.dart' as http;

import '../models/admin.dart';
import '../models/dashboard.dart';
import '../models/task.dart';
import 'token_storage.dart';

/// Thin REST client for the employee mobile-app API added to
/// services/floorwatch-rules-engine/app/main.py (the "Employee
/// mobile-app API" section, right before the Twilio SMS webhook).
///
/// Base URL is NOT hardcoded — set it at build/run time:
///   flutter run --dart-define=FLOORWATCH_API_BASE_URL=https://your-app.up.railway.app
/// Same discipline as this repo's backend config (e.g.
/// FLOORWATCH_PUBLIC_BASE_URL) — never guess a deployment's real URL.
class ApiException implements Exception {
  final int statusCode;
  final String message;
  ApiException(this.statusCode, this.message);
  @override
  String toString() => message;
}

/// package:http has NO default timeout — a stalled connection (as seen
/// on this network: some requests threw ClientException immediately,
/// others just never returned at all) leaves the caller's Future
/// pending forever. That looked like "the button does nothing" — no
/// spinner resolving, no error, because nothing ever threw. Every
/// request below is wrapped in .timeout() so a stall becomes a visible
/// error instead of an infinite hang.
const _requestTimeout = Duration(seconds: 20);

class ApiClient {
  ApiClient._();
  static final ApiClient instance = ApiClient._();

  static const String baseUrl = String.fromEnvironment(
    'FLOORWATCH_API_BASE_URL',
    defaultValue: '',
  );

  Uri _uri(String path) {
    if (baseUrl.isEmpty) {
      throw ApiException(
        0,
        'App is not configured with a server address. '
        'Rebuild with --dart-define=FLOORWATCH_API_BASE_URL=https://your-deployment-url',
      );
    }
    return Uri.parse('$baseUrl$path');
  }

  Future<Map<String, String>> _authHeaders() async {
    final token = await TokenStorage.instance.readToken();
    return {
      'Content-Type': 'application/json',
      if (token != null) 'Authorization': 'Bearer $token',
    };
  }

  /// A dashboard (username/password) session talks to the regular /api/*
  /// endpoints the web dashboard uses; an employee session talks to the
  /// /api/employee/* mirrors of them. Same request/response shapes either
  /// way, so callers below just use the employee paths and this maps them.
  Future<String> _resolvePath(String path) async {
    if (await TokenStorage.instance.readKind() != 'dashboard') return path;
    const rules = [
      ['/api/employee/dashboard/queue/', '/api/queue/'],
      ['/api/employee/dashboard/employees', '/api/admin/employees'],
      ['/api/employee/dashboard/zones', '/api/admin/zones'],
      ['/api/employee/dashboard/history', '/api/history'],
      ['/api/employee/dashboard/', '/api/'],
      ['/api/employee/admin/users', '/api/admin/users'],
    ];
    for (final r in rules) {
      if (path.startsWith(r[0])) return r[1] + path.substring(r[0].length);
    }
    return path;
  }

  Future<http.Response> _get(String path, {required Map<String, String> headers}) async {
    final resolved = await _resolvePath(path);
    return http.get(_uri(resolved), headers: headers).timeout(
          _requestTimeout,
          onTimeout: () => throw ApiException(0, 'Request timed out — check your connection and try again.'),
        );
  }

  Future<http.Response> _post(String path, {required Map<String, String> headers, String? body}) async {
    final resolved = await _resolvePath(path);
    return http.post(_uri(resolved), headers: headers, body: body).timeout(
          _requestTimeout,
          onTimeout: () => throw ApiException(0, 'Request timed out — check your connection and try again.'),
        );
  }

  Future<http.Response> _put(String path, {required Map<String, String> headers, String? body}) async {
    final resolved = await _resolvePath(path);
    return http.put(_uri(resolved), headers: headers, body: body).timeout(
          _requestTimeout,
          onTimeout: () => throw ApiException(0, 'Request timed out — check your connection and try again.'),
        );
  }

  Map<String, dynamic> _decode(http.Response resp) {
    final body = resp.body.isEmpty ? <String, dynamic>{} : jsonDecode(resp.body) as Map<String, dynamic>;
    if (resp.statusCode >= 200 && resp.statusCode < 300) return body;
    final message = (body['error'] ?? body['detail']) as String? ?? 'Request failed (${resp.statusCode})';
    throw ApiException(resp.statusCode, message);
  }

  /// Same error handling as _decode, for the dashboard endpoints that
  /// return a bare JSON array (/api/queue, /api/queue/tasks) rather
  /// than an object.
  List<dynamic> _decodeList(http.Response resp) {
    if (resp.statusCode >= 200 && resp.statusCode < 300) {
      return resp.body.isEmpty ? <dynamic>[] : jsonDecode(resp.body) as List<dynamic>;
    }
    final body = resp.body.isEmpty ? <String, dynamic>{} : jsonDecode(resp.body) as Map<String, dynamic>;
    final message = (body['error'] ?? body['detail']) as String? ?? 'Request failed (${resp.statusCode})';
    throw ApiException(resp.statusCode, message);
  }

  // ── Auth ──────────────────────────────────────────────────────────────

  /// Primary login path. Returns (token, employee_number, name) on
  /// success — caller persists via TokenStorage. OTP below is kept as
  /// infrastructure for a possible future 2FA step, not the main flow.
  Future<Map<String, dynamic>> login(String phone, String password) async {
    final resp = await _post(
      '/api/employee/auth/login',
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'phone': phone, 'password': password}),
    );
    return _decode(resp);
  }

  /// Dashboard-account login (the same username/password as the web
  /// dashboard). Returns {token, username, role, must_change_password}.
  Future<Map<String, dynamic>> dashboardLogin(String username, String password) async {
    final resp = await _post(
      '/api/login',
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'username': username, 'password': password}),
    );
    return _decode(resp);
  }

  Future<void> changeDashboardPassword(String currentPassword, String newPassword) async {
    final resp = await _post(
      '/api/change-password',
      headers: await _authHeaders(),
      body: jsonEncode({'current_password': currentPassword, 'new_password': newPassword}),
    );
    _decode(resp);
  }

  Future<void> changeEmployeePassword(String currentPassword, String newPassword) async {
    final resp = await _post(
      '/api/employee/auth/change-password',
      headers: await _authHeaders(),
      body: jsonEncode({'current_password': currentPassword, 'new_password': newPassword}),
    );
    _decode(resp);
  }

  Future<void> requestOtp(String phone) async {
    final resp = await _post(
      '/api/employee/auth/request-otp',
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'phone': phone}),
    );
    _decode(resp);
  }

  /// Returns (token, employeeNumber, name) on success — caller is
  /// responsible for persisting via TokenStorage.
  Future<Map<String, dynamic>> verifyOtp(String phone, String code) async {
    final resp = await _post(
      '/api/employee/auth/verify-otp',
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'phone': phone, 'code': code}),
    );
    return _decode(resp);
  }

  // ── Tasks ─────────────────────────────────────────────────────────────

  Future<List<EmployeeTask>> fetchTasks() async {
    final resp = await _get('/api/employee/tasks', headers: await _authHeaders());
    final body = _decode(resp);
    final list = body['tasks'] as List<dynamic>;
    return list.map((e) => EmployeeTask.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<void> startTask(String taskId) async {
    final resp = await _post('/api/employee/tasks/$taskId/start', headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> completeTask(String taskId) async {
    final resp = await _post('/api/employee/tasks/$taskId/complete', headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> requestExtension(String taskId) async {
    final resp = await _post('/api/employee/tasks/$taskId/request-extension', headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> requestReview(String taskId) async {
    final resp = await _post('/api/employee/tasks/$taskId/request-review', headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> reassignTask(String taskId, String newAssignee) async {
    final resp = await _post(
      '/api/employee/tasks/$taskId/reassign',
      headers: await _authHeaders(),
      body: jsonEncode({'new_assignee': newAssignee}),
    );
    _decode(resp);
  }

  // ── Supervisor dashboard ─────────────────────────────────────────────
  // Mirrors the web dashboard's own /api/state, /api/tasks, /api/queue*
  // and action endpoints exactly, just under /api/employee/dashboard/
  // and gated on require_employee_supervisor — see main.py.

  Future<List<ZoneState>> fetchZoneStates() async {
    final resp = await _get('/api/employee/dashboard/state', headers: await _authHeaders());
    final body = _decode(resp);
    return body.entries.map((e) => ZoneState.fromJson(e.key, e.value as Map<String, dynamic>)).toList();
  }

  Future<List<DashboardTask>> fetchDashboardTasks() async {
    final resp = await _get('/api/employee/dashboard/tasks', headers: await _authHeaders());
    final body = _decode(resp);
    return body.entries.map((e) => DashboardTask.fromJson(e.key, e.value as Map<String, dynamic>)).toList();
  }

  Future<List<QueueItem>> fetchSupervisorQueue() async {
    final resp = await _get('/api/employee/dashboard/queue/tasks', headers: await _authHeaders());
    final list = _decodeList(resp);
    return list.map((e) => QueueItem.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<List<ZoneDirective>> fetchZoneDirectives() async {
    final resp = await _get('/api/employee/dashboard/queue', headers: await _authHeaders());
    final list = _decodeList(resp);
    return list.map((e) => ZoneDirective.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<void> approveZoneDirective(String zoneId) async {
    final resp =
        await _post('/api/employee/dashboard/queue/zone/$zoneId/approve', headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> reassignZoneCoverage(String zoneId) async {
    final resp =
        await _post('/api/employee/dashboard/queue/zone/$zoneId/reassign', headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> assignTask({
    required String taskName,
    required String zoneId,
    required double assignedMinutes,
    required String taskType,
    String? assignedTo,
  }) async {
    final resp = await _post(
      '/api/employee/dashboard/tasks',
      headers: await _authHeaders(),
      body: jsonEncode({
        'task_name': taskName, 'zone_id': zoneId, 'assigned_minutes': assignedMinutes,
        'task_type': taskType, 'assigned_to': assignedTo,
      }),
    );
    _decode(resp);
  }

  Future<void> confirmFlag(String taskId) async {
    final resp =
        await _post('/api/employee/dashboard/queue/task/$taskId/confirm', headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> dismissFlag(String taskId) async {
    final resp =
        await _post('/api/employee/dashboard/queue/task/$taskId/dismiss', headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> dashboardExtendTask(String taskId, double extraMinutes) async {
    final resp = await _post(
      '/api/employee/dashboard/tasks/$taskId/extend',
      headers: await _authHeaders(),
      body: jsonEncode({'extra_minutes': extraMinutes}),
    );
    _decode(resp);
  }

  Future<void> dashboardResolveReview(String taskId) async {
    final resp =
        await _post('/api/employee/dashboard/tasks/$taskId/resolve-review', headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> dashboardReassignTask(String taskId, String newAssignee) async {
    final resp = await _post(
      '/api/employee/dashboard/tasks/$taskId/reassign',
      headers: await _authHeaders(),
      body: jsonEncode({'new_assignee': newAssignee}),
    );
    _decode(resp);
  }

  // ── Manage Employees (require_employee_supervisor: supervisor OR admin) ─

  Future<List<EmployeeRecord>> fetchEmployees() async {
    final resp = await _get('/api/employee/dashboard/employees', headers: await _authHeaders());
    final body = _decode(resp);
    final list = body['employees'] as List<dynamic>;
    return list.map((e) => EmployeeRecord.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<void> addEmployee({
    required String employeeNumber,
    required String name,
    required String role,
    required String department,
    required String phone,
    bool isPrimaryContact = false,
  }) async {
    final resp = await _post(
      '/api/employee/dashboard/employees',
      headers: await _authHeaders(),
      body: jsonEncode({
        'employee_number': employeeNumber, 'name': name, 'role': role,
        'department': department, 'phone': phone, 'is_primary_contact': isPrimaryContact,
      }),
    );
    _decode(resp);
  }

  Future<void> editEmployee({
    required String employeeNumber,
    required String name,
    required String role,
    required String department,
    required String phone,
    bool isPrimaryContact = false,
  }) async {
    final resp = await _put(
      '/api/employee/dashboard/employees/$employeeNumber',
      headers: await _authHeaders(),
      body: jsonEncode({
        'name': name, 'role': role, 'department': department, 'phone': phone,
        'is_primary_contact': isPrimaryContact,
      }),
    );
    _decode(resp);
  }

  Future<void> deactivateEmployee(String employeeNumber) async {
    final resp = await _post(
      '/api/employee/dashboard/employees/$employeeNumber/deactivate', headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> reactivateEmployee(String employeeNumber) async {
    final resp = await _post(
      '/api/employee/dashboard/employees/$employeeNumber/reactivate', headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> setEmployeePrimaryContact(String employeeNumber, bool isPrimaryContact) async {
    final resp = await _post(
      '/api/employee/dashboard/employees/$employeeNumber/set-primary-contact',
      headers: await _authHeaders(),
      body: jsonEncode({'is_primary_contact': isPrimaryContact}),
    );
    _decode(resp);
  }

  Future<void> setEmployeePassword(String employeeNumber, String password) async {
    final resp = await _post(
      '/api/employee/dashboard/employees/$employeeNumber/set-password',
      headers: await _authHeaders(),
      body: jsonEncode({'password': password}),
    );
    _decode(resp);
  }

  // ── Manage Zones (require_employee_supervisor) ──────────────────────

  Future<List<ZoneRecord>> fetchZones() async {
    final resp = await _get('/api/employee/dashboard/zones', headers: await _authHeaders());
    final body = _decode(resp);
    final list = body['zones'] as List<dynamic>;
    return list.map((e) => ZoneRecord.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<void> addZone({
    required String zoneId,
    required String name,
    required String roleTag,
    bool staffed = true,
  }) async {
    final resp = await _post(
      '/api/employee/dashboard/zones',
      headers: await _authHeaders(),
      body: jsonEncode({'zone_id': zoneId, 'name': name, 'role_tag': roleTag, 'staffed': staffed}),
    );
    _decode(resp);
  }

  Future<void> deactivateZone(String zoneId) async {
    final resp =
        await _post('/api/employee/dashboard/zones/$zoneId/deactivate', headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> reactivateZone(String zoneId) async {
    final resp =
        await _post('/api/employee/dashboard/zones/$zoneId/reactivate', headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> setZoneStaffed(String zoneId, bool staffed) async {
    final resp = await _post(
      '/api/employee/dashboard/zones/$zoneId/set-staffed',
      headers: await _authHeaders(),
      body: jsonEncode({'staffed': staffed}),
    );
    _decode(resp);
  }

  // ── History (require_employee_supervisor) ───────────────────────────

  Future<List<HistoryEvent>> fetchHistory({int limit = 200}) async {
    final resp = await _get('/api/employee/dashboard/history?limit=$limit', headers: await _authHeaders());
    final list = _decodeList(resp);
    return list.map((e) => HistoryEvent.fromJson(e as Map<String, dynamic>)).toList();
  }

  // ── Manage Users — dashboard accounts (require_employee_admin) ──────

  Future<List<DashboardUserAccount>> fetchDashboardUsers() async {
    final resp = await _get('/api/employee/admin/users', headers: await _authHeaders());
    final body = _decode(resp);
    final list = body['users'] as List<dynamic>;
    return list.map((e) => DashboardUserAccount.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<void> createDashboardUser(String username, String password, String role) async {
    final resp = await _post(
      '/api/employee/admin/users',
      headers: await _authHeaders(),
      body: jsonEncode({'username': username, 'password': password, 'role': role}),
    );
    _decode(resp);
  }

  Future<void> deactivateDashboardUser(String username) async {
    final resp =
        await _post('/api/employee/admin/users/$username/deactivate', headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> reactivateDashboardUser(String username) async {
    final resp =
        await _post('/api/employee/admin/users/$username/reactivate', headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> resetDashboardUserPassword(String username, String newPassword) async {
    final resp = await _post(
      '/api/employee/admin/users/$username/reset-password',
      headers: await _authHeaders(),
      body: jsonEncode({'new_password': newPassword}),
    );
    _decode(resp);
  }

  Future<void> dashboardCompleteTask(String taskId) async {
    final resp =
        await _post('/api/employee/dashboard/tasks/$taskId/complete', headers: await _authHeaders());
    _decode(resp);
  }

  // ── Push ──────────────────────────────────────────────────────────────

  Future<void> registerDeviceToken(String fcmToken) async {
    final resp = await _post(
      '/api/employee/device-token',
      headers: await _authHeaders(),
      body: jsonEncode({'fcm_token': fcmToken}),
    );
    _decode(resp);
  }
}
