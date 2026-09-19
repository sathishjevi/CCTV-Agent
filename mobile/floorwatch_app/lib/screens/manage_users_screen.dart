import 'package:flutter/material.dart';

import '../models/admin.dart';
import '../services/api_client.dart';

/// Mirrors the web dashboard's admin-only "Manage Users" panel —
/// dashboard username/password accounts, a different identity system
/// from employee_directory. /api/employee/admin/users*
/// (require_employee_admin — only employee_directory role=="admin").
class ManageUsersScreen extends StatefulWidget {
  const ManageUsersScreen({super.key});

  @override
  State<ManageUsersScreen> createState() => _ManageUsersScreenState();
}

class _ManageUsersScreenState extends State<ManageUsersScreen> {
  List<DashboardUserAccount> _users = [];
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final users = await ApiClient.instance.fetchDashboardUsers();
      if (!mounted) return;
      setState(() => _users = users);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _error = e.message);
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = 'Network error — pull down to retry.');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _runAction(Future<void> Function() action) async {
    try {
      await action();
      await _load();
    } on ApiException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message)));
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('Network error — check your connection and try again.')));
    }
  }

  Future<void> _addUserDialog() async {
    final usernameController = TextEditingController();
    final passwordController = TextEditingController();
    String role = 'supervisor';
    final saved = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          title: const Text('Create dashboard account'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                  controller: usernameController, decoration: const InputDecoration(labelText: 'Username')),
              TextField(
                controller: passwordController,
                obscureText: true,
                decoration: const InputDecoration(labelText: 'Password'),
              ),
              DropdownButtonFormField<String>(
                initialValue: role,
                decoration: const InputDecoration(labelText: 'Role'),
                items: const [
                  DropdownMenuItem(value: 'admin', child: Text('admin')),
                  DropdownMenuItem(value: 'supervisor', child: Text('Secondary Admin')),
                  DropdownMenuItem(value: 'viewer', child: Text('viewer')),
                ],
                onChanged: (v) => setDialogState(() => role = v ?? 'supervisor'),
              ),
            ],
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Create')),
          ],
        ),
      ),
    );
    if (saved != true) return;
    await _runAction(() => ApiClient.instance
        .createDashboardUser(usernameController.text.trim(), passwordController.text, role));
  }

  Future<void> _resetPasswordDialog(String username) async {
    final controller = TextEditingController();
    final password = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('Reset password for $username'),
        content: TextField(controller: controller, obscureText: true, autofocus: true),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, controller.text), child: const Text('Reset')),
        ],
      ),
    );
    if (password == null || password.isEmpty) return;
    await _runAction(() => ApiClient.instance.resetDashboardUserPassword(username, password));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Manage Users'),
        actions: [IconButton(icon: const Icon(Icons.add), onPressed: _addUserDialog)],
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: _loading && _users.isEmpty
            ? const Center(child: CircularProgressIndicator())
            : ListView(
                padding: const EdgeInsets.all(16),
                children: [
                  if (_error != null)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 12),
                      child: Text(_error!, style: const TextStyle(color: Colors.red)),
                    ),
                  ..._users.map((u) => Card(
                        margin: const EdgeInsets.only(bottom: 8),
                        child: Padding(
                          padding: const EdgeInsets.all(12),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(u.username, style: const TextStyle(fontWeight: FontWeight.bold)),
                              Text(u.role == 'supervisor' ? 'Secondary Admin' : u.role),
                              Text(u.active ? 'active' : 'deactivated',
                                  style: TextStyle(color: u.active ? Colors.green : Colors.red)),
                              const SizedBox(height: 8),
                              Wrap(
                                spacing: 8,
                                runSpacing: 8,
                                children: [
                                  OutlinedButton(
                                      onPressed: () => _resetPasswordDialog(u.username),
                                      child: const Text('Reset password')),
                                  if (u.active)
                                    OutlinedButton(
                                        onPressed: () => _runAction(
                                            () => ApiClient.instance.deactivateDashboardUser(u.username)),
                                        child: const Text('Deactivate'))
                                  else
                                    OutlinedButton(
                                        onPressed: () => _runAction(
                                            () => ApiClient.instance.reactivateDashboardUser(u.username)),
                                        child: const Text('Reactivate')),
                                ],
                              ),
                            ],
                          ),
                        ),
                      )),
                ],
              ),
      ),
    );
  }
}
