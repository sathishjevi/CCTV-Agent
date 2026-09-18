import 'dart:convert';
import 'package:http/http.dart' as http;

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

  Future<http.Response> _get(String path, {required Map<String, String> headers}) {
    return http.get(_uri(path), headers: headers).timeout(
          _requestTimeout,
          onTimeout: () => throw ApiException(0, 'Request timed out — check your connection and try again.'),
        );
  }

  Future<http.Response> _post(String path, {required Map<String, String> headers, String? body}) {
    return http.post(_uri(path), headers: headers, body: body).timeout(
          _requestTimeout,
          onTimeout: () => throw ApiException(0, 'Request timed out — check your connection and try again.'),
        );
  }

  Map<String, dynamic> _decode(http.Response resp) {
    final body = resp.body.isEmpty ? <String, dynamic>{} : jsonDecode(resp.body) as Map<String, dynamic>;
    if (resp.statusCode >= 200 && resp.statusCode < 300) return body;
    final message = body['error'] as String? ?? 'Request failed (${resp.statusCode})';
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
