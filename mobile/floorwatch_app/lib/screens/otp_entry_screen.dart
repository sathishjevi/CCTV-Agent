import 'package:flutter/material.dart';

import '../services/api_client.dart';
import '../services/token_storage.dart';
import '../services/push_service.dart';
import 'task_list_screen.dart';

class OtpEntryScreen extends StatefulWidget {
  final String phone;
  const OtpEntryScreen({super.key, required this.phone});

  @override
  State<OtpEntryScreen> createState() => _OtpEntryScreenState();
}

class _OtpEntryScreenState extends State<OtpEntryScreen> {
  final _codeController = TextEditingController();
  bool _verifying = false;
  String? _error;

  Future<void> _verify() async {
    final code = _codeController.text.trim();
    if (code.isEmpty) {
      setState(() => _error = 'Enter the code we sent you.');
      return;
    }
    setState(() {
      _verifying = true;
      _error = null;
    });
    try {
      final result = await ApiClient.instance.verifyOtp(widget.phone, code);
      await TokenStorage.instance.save(
        token: result['token'] as String,
        employeeNumber: result['employee_number'] as String,
        name: result['name'] as String,
      );
      // Best-effort — push not being configured yet (no Firebase project
      // wired up) must never block login. See push_service.dart's
      // docstring for what's required before this actually delivers push.
      try {
        await PushService.instance.initialize();
      } catch (_) {}
      if (!mounted) return;
      Navigator.of(context).pushAndRemoveUntil(
        MaterialPageRoute(builder: (_) => const TaskListScreen()),
        (route) => false,
      );
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _verifying = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Enter code')),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text('We sent a code to ${widget.phone}.'),
              const SizedBox(height: 24),
              TextField(
                controller: _codeController,
                keyboardType: TextInputType.number,
                maxLength: 6,
                decoration: const InputDecoration(labelText: 'Login code', border: OutlineInputBorder()),
              ),
              if (_error != null) ...[
                const SizedBox(height: 8),
                Text(_error!, style: const TextStyle(color: Colors.red)),
              ],
              const SizedBox(height: 16),
              FilledButton(
                onPressed: _verifying ? null : _verify,
                child: _verifying
                    ? const SizedBox(height: 20, width: 20, child: CircularProgressIndicator(strokeWidth: 2))
                    : const Text('Verify & log in'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
