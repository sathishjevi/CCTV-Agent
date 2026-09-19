import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Wraps the platform keychain/keystore for the employee's JWT — never
/// stored in plain SharedPreferences, since this token authenticates
/// every task action the app can take on the employee's behalf.
class TokenStorage {
  TokenStorage._();
  static final TokenStorage instance = TokenStorage._();

  final _storage = const FlutterSecureStorage();
  static const _tokenKey = 'floorwatch_employee_token';
  static const _employeeNumberKey = 'floorwatch_employee_number';
  static const _employeeNameKey = 'floorwatch_employee_name';
  // employee_directory role ("employee" | "supervisor") — purely a
  // client-side UI signal for whether to show the Dashboard tab; the
  // token itself is always role="employee" server-side regardless (see
  // require_employee_supervisor's docstring in main.py).
  static const _roleKey = 'floorwatch_employee_role';

  Future<void> save({
    required String token,
    required String employeeNumber,
    required String name,
    required String role,
  }) async {
    await _storage.write(key: _tokenKey, value: token);
    await _storage.write(key: _employeeNumberKey, value: employeeNumber);
    await _storage.write(key: _employeeNameKey, value: name);
    await _storage.write(key: _roleKey, value: role);
  }

  Future<String?> readToken() => _storage.read(key: _tokenKey);

  Future<String?> readEmployeeName() => _storage.read(key: _employeeNameKey);

  Future<String?> readRole() => _storage.read(key: _roleKey);

  Future<void> clear() async {
    await _storage.delete(key: _tokenKey);
    await _storage.delete(key: _employeeNumberKey);
    await _storage.delete(key: _employeeNameKey);
    await _storage.delete(key: _roleKey);
  }
}
