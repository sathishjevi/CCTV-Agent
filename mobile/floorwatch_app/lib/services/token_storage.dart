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

  Future<void> save({
    required String token,
    required String employeeNumber,
    required String name,
  }) async {
    await _storage.write(key: _tokenKey, value: token);
    await _storage.write(key: _employeeNumberKey, value: employeeNumber);
    await _storage.write(key: _employeeNameKey, value: name);
  }

  Future<String?> readToken() => _storage.read(key: _tokenKey);

  Future<String?> readEmployeeName() => _storage.read(key: _employeeNameKey);

  Future<void> clear() async {
    await _storage.delete(key: _tokenKey);
    await _storage.delete(key: _employeeNumberKey);
    await _storage.delete(key: _employeeNameKey);
  }
}
