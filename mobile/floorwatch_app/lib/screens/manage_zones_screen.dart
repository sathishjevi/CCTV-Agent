import 'package:flutter/material.dart';

import '../models/admin.dart';
import '../services/api_client.dart';

/// Mirrors the web dashboard's "Manage Zones" panel, against
/// /api/employee/dashboard/zones* (require_employee_supervisor).
class ManageZonesScreen extends StatefulWidget {
  const ManageZonesScreen({super.key});

  @override
  State<ManageZonesScreen> createState() => _ManageZonesScreenState();
}

class _ManageZonesScreenState extends State<ManageZonesScreen> {
  List<ZoneRecord> _zones = [];
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
      final zones = await ApiClient.instance.fetchZones();
      if (!mounted) return;
      setState(() => _zones = zones);
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

  Future<void> _addZoneDialog() async {
    final idController = TextEditingController();
    final nameController = TextEditingController();
    final roleTagController = TextEditingController();
    final saved = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Add zone'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(controller: idController, decoration: const InputDecoration(labelText: 'Zone ID')),
            TextField(controller: nameController, decoration: const InputDecoration(labelText: 'Name')),
            TextField(
                controller: roleTagController, decoration: const InputDecoration(labelText: 'Role tag')),
          ],
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Add')),
        ],
      ),
    );
    if (saved != true) return;
    await _runAction(() => ApiClient.instance.addZone(
          zoneId: idController.text.trim(),
          name: nameController.text.trim(),
          roleTag: roleTagController.text.trim(),
        ));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Manage Zones'),
        actions: [IconButton(icon: const Icon(Icons.add), onPressed: _addZoneDialog)],
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: _loading && _zones.isEmpty
            ? const Center(child: CircularProgressIndicator())
            : ListView(
                padding: const EdgeInsets.all(16),
                children: [
                  if (_error != null)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 12),
                      child: Text(_error!, style: const TextStyle(color: Colors.red)),
                    ),
                  ..._zones.map((z) => Card(
                        margin: const EdgeInsets.only(bottom: 8),
                        child: Padding(
                          padding: const EdgeInsets.all(12),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text('${z.name} (${z.zoneId})', style: const TextStyle(fontWeight: FontWeight.bold)),
                              Text('Role tag: ${z.roleTag}'),
                              Text(z.active ? 'active' : 'deactivated',
                                  style: TextStyle(color: z.active ? Colors.green : Colors.red)),
                              const SizedBox(height: 8),
                              // Security alerts (security_engine.py): when should a person here raise an alert,
                              // and is this a way into the building?
                              DropdownButtonFormField<String>(
                                key: ValueKey('${z.zoneId}-access-${z.access}'),
                                initialValue: z.access,
                                isExpanded: true,
                                decoration: const InputDecoration(labelText: 'Security rule', isDense: true),
                                items: const [
                                  DropdownMenuItem(value: 'open', child: Text('No rule')),
                                  DropdownMenuItem(value: 'closed_hours', child: Text('Alert when closed')),
                                  DropdownMenuItem(value: 'restricted', child: Text('Off-limits (always alert)')),
                                ],
                                onChanged: (v) => v == null
                                    ? null
                                    : _runAction(() => ApiClient.instance.setZoneSecurity(z.zoneId, access: v)),
                              ),
                              const SizedBox(height: 8),
                              DropdownButtonFormField<String>(
                                key: ValueKey('${z.zoneId}-entry-${z.entryPoint}'),
                                initialValue: z.entryPoint,
                                isExpanded: true,
                                decoration: const InputDecoration(labelText: 'Entry point', isDense: true),
                                items: const [
                                  DropdownMenuItem(value: '', child: Text('Not an entry point')),
                                  DropdownMenuItem(value: 'door', child: Text('Door (break-in when closed)')),
                                  DropdownMenuItem(value: 'window', child: Text('Window (break-in when closed)')),
                                ],
                                onChanged: (v) => v == null
                                    ? null
                                    : _runAction(() => ApiClient.instance.setZoneSecurity(z.zoneId, entryPoint: v)),
                              ),
                              const SizedBox(height: 8),
                              Wrap(
                                spacing: 8,
                                runSpacing: 8,
                                children: [
                                  SwitchListTile(
                                    contentPadding: EdgeInsets.zero,
                                    dense: true,
                                    value: z.staffed,
                                    title: const Text('Staffed'),
                                    onChanged: (v) =>
                                        _runAction(() => ApiClient.instance.setZoneStaffed(z.zoneId, v)),
                                  ),
                                  if (z.active)
                                    OutlinedButton(
                                        onPressed: () =>
                                            _runAction(() => ApiClient.instance.deactivateZone(z.zoneId)),
                                        child: const Text('Deactivate'))
                                  else
                                    OutlinedButton(
                                        onPressed: () =>
                                            _runAction(() => ApiClient.instance.reactivateZone(z.zoneId)),
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
