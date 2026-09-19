import 'package:flutter/material.dart';

import '../services/api_client.dart';
import '../services/token_storage.dart';

/// Self-service password change for whoever is logged in — a phone login
/// changes the employee password, an admin (username) login changes the
/// dashboard-account password. Both require the current password.
Future<void> showChangePasswordDialog(BuildContext context) async {
  final currentController = TextEditingController();
  final newController = TextEditingController();
  final kind = await TokenStorage.instance.readKind();
  if (!context.mounted) return;
  String? error;
  bool busy = false;

  final changed = await showDialog<bool>(
    context: context,
    builder: (context) => StatefulBuilder(
      builder: (context, setDialogState) => AlertDialog(
        title: const Text('Change password'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: currentController,
              obscureText: true,
              autofocus: true,
              decoration: const InputDecoration(labelText: 'Current password'),
            ),
            TextField(
              controller: newController,
              obscureText: true,
              decoration: const InputDecoration(labelText: 'New password'),
            ),
            if (error != null)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(error!, style: const TextStyle(color: Colors.red)),
              ),
          ],
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(
            onPressed: busy
                ? null
                : () async {
                    setDialogState(() {
                      busy = true;
                      error = null;
                    });
                    try {
                      if (kind == 'dashboard') {
                        await ApiClient.instance
                            .changeDashboardPassword(currentController.text, newController.text);
                      } else {
                        await ApiClient.instance
                            .changeEmployeePassword(currentController.text, newController.text);
                      }
                      if (context.mounted) Navigator.pop(context, true);
                    } on ApiException catch (e) {
                      setDialogState(() {
                        busy = false;
                        error = e.message;
                      });
                    } catch (_) {
                      setDialogState(() {
                        busy = false;
                        error = 'Network error — try again.';
                      });
                    }
                  },
            child: const Text('Change'),
          ),
        ],
      ),
    ),
  );

  if (changed == true && context.mounted) {
    ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Password changed.')));
  }
}
