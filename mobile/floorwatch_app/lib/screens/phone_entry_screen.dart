import 'package:flutter/material.dart';

import '../services/api_client.dart';
import '../services/push_service.dart';
import '../services/token_storage.dart';
import 'admin_login_screen.dart';
import 'supervisor_home_screen.dart';
import 'task_list_screen.dart';

/// Primary login screen: phone + password (see EmployeeLoginRequest in
/// main.py). OTP (otp_entry_screen.dart) is kept as infrastructure for a
/// possible future 2FA step, not wired into this flow.
class PhoneEntryScreen extends StatefulWidget {
  const PhoneEntryScreen({super.key});

  @override
  State<PhoneEntryScreen> createState() => _PhoneEntryScreenState();
}

class _PhoneEntryScreenState extends State<PhoneEntryScreen> {
  final _phoneController = TextEditingController();
  final _passwordController = TextEditingController();
  bool _loggingIn = false;
  String? _error;

  Future<void> _login() async {
    final phone = _phoneController.text.trim();
    final password = _passwordController.text;
    if (phone.isEmpty || password.isEmpty) {
      setState(() => _error = 'Enter your phone number and password.');
      return;
    }
    setState(() {
      _loggingIn = true;
      _error = null;
    });
    try {
      final result = await ApiClient.instance.login(phone, password);
      final role = result['role'] as String? ?? 'employee';
      await TokenStorage.instance.save(
        token: result['token'] as String,
        employeeNumber: result['employee_number'] as String,
        name: result['name'] as String,
        role: role,
      );
      // Best-effort — push not being configured yet must never block login.
      try {
        await PushService.instance.initialize();
      } catch (_) {}
      if (!mounted) return;
      Navigator.of(context).pushAndRemoveUntil(
        MaterialPageRoute(
          builder: (_) => (role == 'supervisor' || role == 'admin')
              ? const SupervisorHomeScreen()
              : const TaskListScreen(),
        ),
        (route) => false,
      );
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } catch (e) {
      // A dropped connection (ClientException/SocketException) isn't an
      // ApiException — must still be shown, not swallowed silently.
      if (mounted) setState(() => _error = 'Network error — check your connection and try again.');
    } finally {
      if (mounted) setState(() => _loggingIn = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Stack(children: [
          Positioned(
            top: 4,
            right: 8,
            child: TextButton(
              onPressed: () => Navigator.of(context)
                  .push(MaterialPageRoute(builder: (_) => const AdminLoginScreen())),
              child: const Text('Admin login'),
            ),
          ),
          Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text('Floorwatch', style: TextStyle(fontSize: 32, fontWeight: FontWeight.bold)),
              const SizedBox(height: 8),
              const Text('Log in with your phone number and password.'),
              const SizedBox(height: 24),
              TextField(
                controller: _phoneController,
                keyboardType: TextInputType.phone,
                decoration: const InputDecoration(
                  labelText: 'Phone number',
                  hintText: '+1 555 000 0101',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _passwordController,
                obscureText: true,
                onSubmitted: (_) => _loggingIn ? null : _login(),
                decoration: const InputDecoration(
                  labelText: 'Password',
                  border: OutlineInputBorder(),
                ),
              ),
              if (_error != null) ...[
                const SizedBox(height: 8),
                Text(_error!, style: const TextStyle(color: Colors.red)),
              ],
              const SizedBox(height: 16),
              FilledButton(
                onPressed: _loggingIn ? null : _login,
                child: _loggingIn
                    ? const SizedBox(height: 20, width: 20, child: CircularProgressIndicator(strokeWidth: 2))
                    : const Text('Log in'),
              ),
            ],
          ),
        ),
        ]),
      ),
    );
  }
}
