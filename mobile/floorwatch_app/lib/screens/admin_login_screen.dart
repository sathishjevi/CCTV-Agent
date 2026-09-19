import 'package:flutter/material.dart';

import '../services/api_client.dart';
import '../services/token_storage.dart';
import 'supervisor_home_screen.dart';

/// Username + password login for dashboard accounts — the same accounts the
/// web dashboard signs in with (admin / Secondary Admin / viewer). Reached
/// from the "Admin login" link on the phone-login screen. The resulting
/// session uses the regular /api/* endpoints (see ApiClient._resolvePath)
/// and has no employee identity, so there is no "My Tasks" tab for it.
class AdminLoginScreen extends StatefulWidget {
  const AdminLoginScreen({super.key});

  @override
  State<AdminLoginScreen> createState() => _AdminLoginScreenState();
}

class _AdminLoginScreenState extends State<AdminLoginScreen> {
  final _usernameController = TextEditingController();
  final _passwordController = TextEditingController();
  bool _loggingIn = false;
  String? _error;

  Future<void> _login() async {
    final username = _usernameController.text.trim();
    final password = _passwordController.text;
    if (username.isEmpty || password.isEmpty) {
      setState(() => _error = 'Enter your username and password.');
      return;
    }
    setState(() {
      _loggingIn = true;
      _error = null;
    });
    try {
      final result = await ApiClient.instance.dashboardLogin(username, password);
      final role = result['role'] as String? ?? 'viewer';
      await TokenStorage.instance.save(
        token: result['token'] as String,
        employeeNumber: username,
        name: username,
        role: role,
        kind: 'dashboard',
      );
      if (result['must_change_password'] == true) {
        final changed = await _forceChangePassword(password);
        if (!changed) {
          await TokenStorage.instance.clear();
          return;
        }
      }
      if (!mounted) return;
      Navigator.of(context).pushAndRemoveUntil(
        MaterialPageRoute(builder: (_) => const SupervisorHomeScreen(dashboardLogin: true)),
        (route) => false,
      );
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } catch (e) {
      if (mounted) setState(() => _error = 'Network error — check your connection and try again.');
    } finally {
      if (mounted) setState(() => _loggingIn = false);
    }
  }

  /// An admin-issued temporary password can't stay permanent — same rule as
  /// the web dashboard, so the new password is collected here before entry.
  Future<bool> _forceChangePassword(String currentPassword) async {
    final controller = TextEditingController();
    String? dialogError;
    final ok = await showDialog<bool>(
      context: context,
      barrierDismissible: false,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          title: const Text('Set a new password'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Text('Your password was set by an administrator. Choose a new one to continue.'),
              TextField(
                controller: controller,
                obscureText: true,
                autofocus: true,
                decoration: const InputDecoration(labelText: 'New password'),
              ),
              if (dialogError != null)
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: Text(dialogError!, style: const TextStyle(color: Colors.red)),
                ),
            ],
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            FilledButton(
              onPressed: () async {
                try {
                  await ApiClient.instance.changeDashboardPassword(currentPassword, controller.text);
                  if (context.mounted) Navigator.pop(context, true);
                } on ApiException catch (e) {
                  setDialogState(() => dialogError = e.message);
                } catch (_) {
                  setDialogState(() => dialogError = 'Network error — try again.');
                }
              },
              child: const Text('Change'),
            ),
          ],
        ),
      ),
    );
    return ok == true;
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Admin login')),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text('Floorwatch', style: TextStyle(fontSize: 32, fontWeight: FontWeight.bold)),
              const SizedBox(height: 8),
              const Text('Sign in with your dashboard username and password.'),
              const SizedBox(height: 24),
              TextField(
                controller: _usernameController,
                autocorrect: false,
                decoration: const InputDecoration(labelText: 'Username', border: OutlineInputBorder()),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _passwordController,
                obscureText: true,
                onSubmitted: (_) => _loggingIn ? null : _login(),
                decoration: const InputDecoration(labelText: 'Password', border: OutlineInputBorder()),
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
      ),
    );
  }
}
