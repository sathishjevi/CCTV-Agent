import 'package:flutter/material.dart';

import '../models/admin.dart';
import '../services/api_client.dart';

/// Mirrors the web dashboard's "Manage Employees" panel — add/edit/
/// deactivate/reactivate/set password, against
/// /api/employee/dashboard/employees* (require_employee_supervisor).
class ManageEmployeesScreen extends StatefulWidget {
  const ManageEmployeesScreen({super.key});

  @override
  State<ManageEmployeesScreen> createState() => _ManageEmployeesScreenState();
}

class _ManageEmployeesScreenState extends State<ManageEmployeesScreen> {
  List<EmployeeRecord> _employees = [];
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
      final employees = await ApiClient.instance.fetchEmployees();
      if (!mounted) return;
      setState(() => _employees = employees);
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

  Future<void> _openEmployeeForm({EmployeeRecord? existing}) async {
    final numberController = TextEditingController(text: existing?.employeeNumber ?? '');
    final nameController = TextEditingController(text: existing?.name ?? '');
    final deptController = TextEditingController(text: existing?.department ?? '');
    final phoneController = TextEditingController(text: existing?.phone ?? '');
    String role = existing?.role ?? 'employee';
    bool isPrimaryContact = existing?.isPrimaryContact ?? false;

    final saved = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          title: Text(existing == null ? 'Add employee' : 'Edit employee #${existing.employeeNumber}'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (existing == null)
                  TextField(
                    controller: numberController,
                    decoration: const InputDecoration(labelText: 'Employee number'),
                  ),
                TextField(controller: nameController, decoration: const InputDecoration(labelText: 'Name')),
                DropdownButtonFormField<String>(
                  initialValue: role,
                  decoration: const InputDecoration(labelText: 'Role'),
                  items: const [
                    DropdownMenuItem(value: 'employee', child: Text('employee')),
                    DropdownMenuItem(value: 'supervisor', child: Text('supervisor')),
                    DropdownMenuItem(value: 'secondary_admin', child: Text('Secondary Admin')),
                    DropdownMenuItem(value: 'admin', child: Text('admin')),
                  ],
                  onChanged: (v) => setDialogState(() => role = v ?? 'employee'),
                ),
                TextField(
                    controller: deptController, decoration: const InputDecoration(labelText: 'Department')),
                TextField(controller: phoneController, decoration: const InputDecoration(labelText: 'Phone')),
                if (role == 'supervisor')
                  CheckboxListTile(
                    value: isPrimaryContact,
                    title: const Text('Primary contact'),
                    onChanged: (v) => setDialogState(() => isPrimaryContact = v ?? false),
                  ),
              ],
            ),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Save')),
          ],
        ),
      ),
    );
    if (saved != true) return;

    if (existing == null) {
      await _runAction(() => ApiClient.instance.addEmployee(
            employeeNumber: numberController.text.trim(),
            name: nameController.text.trim(),
            role: role,
            department: deptController.text.trim(),
            phone: phoneController.text.trim(),
            isPrimaryContact: isPrimaryContact,
          ));
    } else {
      await _runAction(() => ApiClient.instance.editEmployee(
            employeeNumber: existing.employeeNumber,
            name: nameController.text.trim(),
            role: role,
            department: deptController.text.trim(),
            phone: phoneController.text.trim(),
            isPrimaryContact: isPrimaryContact,
          ));
    }
  }

  Future<void> _setPasswordDialog(String employeeNumber) async {
    final controller = TextEditingController();
    final password = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('Set password for #$employeeNumber'),
        content: TextField(controller: controller, obscureText: true, autofocus: true),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, controller.text), child: const Text('Set')),
        ],
      ),
    );
    if (password == null || password.isEmpty) return;
    await _runAction(() => ApiClient.instance.setEmployeePassword(employeeNumber, password));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Manage Employees'),
        actions: [IconButton(icon: const Icon(Icons.add), onPressed: () => _openEmployeeForm())],
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: _loading && _employees.isEmpty
            ? const Center(child: CircularProgressIndicator())
            : ListView(
                padding: const EdgeInsets.all(16),
                children: [
                  if (_error != null)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 12),
                      child: Text(_error!, style: const TextStyle(color: Colors.red)),
                    ),
                  ..._employees.map((e) => Card(
                        margin: const EdgeInsets.only(bottom: 8),
                        child: Padding(
                          padding: const EdgeInsets.all(12),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text('${e.name} (#${e.employeeNumber})',
                                  style: const TextStyle(fontWeight: FontWeight.bold)),
                              Text('${e.role == 'secondary_admin' ? 'Secondary Admin' : e.role} · ${e.department} · ${e.phone}'
                                  '${e.isPrimaryContact ? " · primary contact" : ""}'),
                              Text(e.active ? 'active' : 'deactivated',
                                  style: TextStyle(color: e.active ? Colors.green : Colors.red)),
                              const SizedBox(height: 8),
                              Wrap(
                                spacing: 8,
                                runSpacing: 8,
                                children: [
                                  OutlinedButton(
                                      onPressed: () => _openEmployeeForm(existing: e),
                                      child: const Text('Edit')),
                                  OutlinedButton(
                                      onPressed: () => _setPasswordDialog(e.employeeNumber),
                                      child: const Text('Set password')),
                                  if (e.active)
                                    OutlinedButton(
                                        onPressed: () => _runAction(
                                            () => ApiClient.instance.deactivateEmployee(e.employeeNumber)),
                                        child: const Text('Deactivate'))
                                  else
                                    OutlinedButton(
                                        onPressed: () => _runAction(
                                            () => ApiClient.instance.reactivateEmployee(e.employeeNumber)),
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
