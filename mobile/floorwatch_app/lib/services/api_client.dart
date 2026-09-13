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

  Map<String, dynamic> _decode(http.Response resp) {
    final body = resp.body.isEmpty ? <String, dynamic>{} : jsonDecode(resp.body) as Map<String, dynamic>;
    if (resp.statusCode >= 200 && resp.statusCode < 300) return body;
    final message = body['error'] as String? ?? 'Request failed (${resp.statusCode})';
    throw ApiException(resp.statusCode, message);
  }

  // ── Auth ──────────────────────────────────────────────────────────────

  Future<void> requestOtp(String phone) async {
    final resp = await http.post(
      _uri('/api/employee/auth/request-otp'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'phone': phone}),
    );
    _decode(resp);
  }

  /// Returns (token, employeeNumber, name) on success — caller is
  /// responsible for persisting via TokenStorage.
  Future<Map<String, dynamic>> verifyOtp(String phone, String code) async {
    final resp = await http.post(
      _uri('/api/employee/auth/verify-otp'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'phone': phone, 'code': code}),
    );
    return _decode(resp);
  }

  // ── Tasks ─────────────────────────────────────────────────────────────

  Future<List<EmployeeTask>> fetchTasks() async {
    final resp = await http.get(_uri('/api/employee/tasks'), headers: await _authHeaders());
    final body = _decode(resp);
    final list = body['tasks'] as List<dynamic>;
    return list.map((e) => EmployeeTask.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<void> startTask(String taskId) async {
    final resp = await http.post(_uri('/api/employee/tasks/$taskId/start'), headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> completeTask(String taskId) async {
    final resp = await http.post(_uri('/api/employee/tasks/$taskId/complete'), headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> requestExtension(String taskId) async {
    final resp =
        await http.post(_uri('/api/employee/tasks/$taskId/request-extension'), headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> requestReview(String taskId) async {
    final resp = await http.post(_uri('/api/employee/tasks/$taskId/request-review'), headers: await _authHeaders());
    _decode(resp);
  }

  Future<void> reassignTask(String taskId, String newAssignee) async {
    final resp = await http.post(
      _uri('/api/employee/tasks/$taskId/reassign'),
      headers: await _authHeaders(),
      body: jsonEncode({'new_assignee': newAssignee}),
    );
    _decode(resp);
  }

  // ── Push ──────────────────────────────────────────────────────────────

  Future<void> registerDeviceToken(String fcmToken) async {
    final resp = await http.post(
      _uri('/api/employee/device-token'),
      headers: await _authHeaders(),
      body: jsonEncode({'fcm_token': fcmToken}),
    );
    _decode(resp);
  }
}
